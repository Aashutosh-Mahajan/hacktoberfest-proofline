"""Model gateway: OllamaGateway (Gemma 4 via a local Ollama server). No scripted/fake model lives in the product."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .compact import compact_messages
from .util import parse_json_loose, trunc

log = logging.getLogger("mayday.agent.gateway")


class ModelError(Exception):
    pass


class ModelTimeout(ModelError):
    pass


class ModelUnavailable(ModelError):
    pass


class ModelAborted(ModelError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    thinking: str = ""
    via: str = ""                # which model produced this reply (e.g. "gemma4:e4b")
    stats: dict = field(default_factory=dict)


class Gateway:
    backend = "ollama"
    name = "base"

    def chat(self, messages: list, tools: Optional[list] = None, *, think: bool = False, timeout: float = 30.0,
             on_text: Optional[Callable[[str], None]] = None, require_tool: bool = False, **kw) -> ModelReply:
        raise NotImplementedError

    def status(self) -> dict:
        return {"backend": self.backend, "name": self.name, "healthy": True, "warm": True}

    def warm_up(self) -> dict:
        return self.status()

    def abort(self) -> None:
        pass

    def supports_audio(self) -> bool:
        return False

    def transcribe(self, audio: bytes, mime: str) -> dict:
        raise ModelError("audio input not supported by this gateway; use browser speech-to-text and POST /api/ask")

    def extract_bulletin(self, info: dict, bank: str) -> dict:
        ex = info.get("extracted")
        if not ex:
            raise ModelError("no bulletin metadata available")
        return ex

    def read_bulletin(self, info: dict, bank: str, ref_ts: Optional[int] = None) -> tuple:
        return self.extract_bulletin(info, bank), "from bulletin metadata"


# --------------------------------------------------------------------------- Ollama
class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown_s: float = 20.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown_s, clock
        self.fails = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.cooldown:
            return True  # half-open: allow a probe
        return False

    def ok(self) -> None:
        self.fails, self.opened_at = 0, None

    def fail(self) -> None:
        self.fails += 1
        if self.fails >= self.threshold:
            self.opened_at = self.clock()

    @property
    def open(self) -> bool:
        return not self.allow()


BULLETIN_SCHEMA = {"type": "object", "properties": {
    "bank": {"type": "string"}, "status": {"type": "string", "enum": ["operational", "degraded", "outage"]},
    "start": {"type": "string", "description": "HH:MM:SS"}, "end": {"type": "string", "description": "HH:MM:SS"}},
    "required": ["bank", "status", "start", "end"]}
BULLETIN_PROMPT = ("This is a bank status bulletin. Extract: bank name (like BANK_X), status (operational/degraded/outage), "
                   "and the claimed window start and end times as HH:MM:SS. Reply with JSON only.")

_CALL_RE = re.compile(r"(?:call:)?\s*([a-z_]+)\s*[({]\s*(.*?)\s*[)}]\s*$", re.S)


def _hms_to_ts(hms: str, ref_ts: int, near: Optional[int] = None) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", str(hms))
    if not m:
        return None
    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    day0 = (int(ref_ts) // 1000) // 86400 * 86400
    anchor = near if near is not None else ref_ts
    cands = [(day0 + k * 86400 + sec) * 1000 for k in (-1, 0, 1)]
    return min(cands, key=lambda c: abs(c - anchor))


class OllamaGateway(Gateway):
    backend = "ollama"

    def __init__(self, url: Optional[str] = None, model: Optional[str] = None, client=None):
        import httpx
        self.url = (url or os.environ.get("MAYDAY_MODEL_URL") or "http://localhost:11434").rstrip("/")
        self.name = model or os.environ.get("MAYDAY_MODEL_NAME") or "gemma4:e4b"
        self.client = client or httpx.Client()
        self.breaker = CircuitBreaker()
        self.warm = False
        self._httpx = httpx
        self._think_ok = True
        self._caps: Optional[set] = None
        self._resp = None
        self._aborted = False
        self._reach: Optional[bool] = None
        self._reach_at = 0.0
        self.calls: list = []          # per-call stats, newest last

    # ---- health / capabilities
    def health(self) -> bool:
        return self.check(2.0)

    def capabilities(self) -> set:
        if self._caps is None:
            try:
                r = self.client.post(self.url + "/api/show", json={"model": self.name}, timeout=5.0)
                self._caps = set(r.json().get("capabilities") or [])
            except Exception:
                return set()
        return self._caps

    def check(self, timeout: float = 1.5) -> bool:
        """Reachability probe (does the server serve our model?). Updates the cached status."""
        try:
            r = self.client.get(self.url + "/api/tags", timeout=timeout)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            ok = any(n == self.name or n.split(":")[0] == self.name.split(":")[0] and ":" not in self.name for n in names)
            self._reach = ok
        except Exception:
            self._reach = False
        self._reach_at = time.monotonic()
        return bool(self._reach)

    def list_models(self) -> list:
        try:
            r = self.client.get(self.url + "/api/tags", timeout=2.0)
            return sorted(m.get("name", "") for m in r.json().get("models", []))
        except Exception:
            return []

    def set_model(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("model name required")
        installed = self.list_models()
        if installed and name not in installed:
            raise ValueError(f"model {name!r} is not installed in Ollama (have: {', '.join(installed)})")
        self.name, self._caps, self.warm, self._think_ok = name, None, False, True
        self.breaker.ok()
        self.check()
        return self.status()

    def status(self) -> dict:
        if self._reach is False and time.monotonic() - self._reach_at > 10.0:
            self.check(1.0)   # recover automatically once the server comes back
        up = self._reach is not False and not self.breaker.open
        return {"backend": "ollama", "name": self.name if up else f"Gemma unavailable ({self.name})", "healthy": up, "warm": self.warm and up}

    def warm_up(self) -> dict:
        try:
            self._call([{"role": "user", "content": "ping"}], None, False, 240.0, num_predict=4)
            self.warm = True
        except ModelError:
            self.warm = False
        return self.status()

    def abort(self) -> None:
        self._aborted = True
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    def supports_audio(self) -> bool:
        return "audio" in self.capabilities() or os.environ.get("MAYDAY_FORCE_AUDIO") == "1"

    def transcribe(self, audio: bytes, mime: str) -> dict:
        """Best effort: Ollama's native /api/chat has no audio field; try the OpenAI-compatible input_audio part."""
        fmt = "wav" if "wav" in mime else ("mp3" if "mp3" in mime or "mpeg" in mime else mime.split("/")[-1].split(";")[0])
        body = {"model": self.name, "stream": False, "messages": [{"role": "user", "content": [
            {"type": "text", "text": 'Transcribe this audio exactly. Reply as JSON {"transcript": str, "lang": "en|hi|hinglish"}.'},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]}]}
        try:
            r = self.client.post(self.url + "/v1/chat/completions", json=body, timeout=60.0)
            if r.status_code >= 400:
                raise ModelError(f"HTTP {r.status_code}: {trunc(r.text, 120)}")
            txt = r.json()["choices"][0]["message"]["content"]
        except ModelError:
            raise
        except Exception as e:
            raise ModelError(f"audio request failed: {e}") from e
        obj = parse_json_loose(txt)
        if isinstance(obj, dict) and obj.get("transcript"):
            return {"transcript": str(obj["transcript"]), "lang": obj.get("lang") or "auto"}
        if txt.strip():
            return {"transcript": txt.strip(), "lang": "auto"}
        raise ModelError("model returned no transcript")

    # ---- vision
    def read_bulletin(self, info: dict, bank: str, ref_ts: Optional[int] = None) -> tuple:
        meta = info.get("extracted")
        png = info.get("png_path")
        if not png or not os.path.exists(png):
            return self.extract_bulletin(info, bank), "from bulletin metadata (no image file)"
        with open(png, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = self._call([{"role": "user", "content": BULLETIN_PROMPT, "images": [b64]}], None, False,
                       float(os.environ.get("MAYDAY_VISION_TIMEOUT_S", "90")), fmt=BULLETIN_SCHEMA, num_predict=100)
        obj = parse_json_loose(r.text)
        if not isinstance(obj, dict) or not obj.get("status"):
            raise ModelError("vision output unparseable")
        ref = int(ref_ts or (meta or {}).get("claimed_start_ts") or time.time() * 1000)
        near = (meta or {}).get("claimed_start_ts")
        cs, ce = _hms_to_ts(obj.get("start", ""), ref, near), _hms_to_ts(obj.get("end", ""), ref, near)
        if cs is None or ce is None:
            raise ModelError("vision output has no readable window")
        if ce < cs:
            ce += 86400_000
        ex = {"bank": str(obj.get("bank") or bank).strip().upper().replace(" ", "_"), "status": str(obj["status"]).lower(),
              "claimed_start_ts": cs, "claimed_end_ts": ce}
        tag = f"read by {self.name} vision"
        if meta:
            diffs = []
            if ex["bank"] != meta.get("bank"):
                diffs.append(f"bank {ex['bank']}!={meta.get('bank')}")
            if ex["status"] != str(meta.get("status", "")).lower():
                diffs.append(f"status {ex['status']}!={meta.get('status')}")
            if abs(cs - meta["claimed_start_ts"]) > 2000 or abs(ce - meta["claimed_end_ts"]) > 2000:
                diffs.append("window times differ")
            if diffs:
                return meta, f"vision misread ({'; '.join(diffs)}); used bulletin metadata"
        return ex, tag

    # ---- core call
    def _call(self, messages, tools, think, timeout, on_text=None, on_think=None, should_stop=None, num_predict=None,
              fmt=None) -> ModelReply:
        if not self.breaker.allow():
            raise ModelUnavailable("circuit breaker open")
        num_ctx = int(os.environ.get("MAYDAY_NUM_CTX", "4096"))
        opts = {"temperature": 0.2, "num_ctx": num_ctx}
        if os.environ.get("MAYDAY_NUM_GPU"):
            opts["num_gpu"] = int(os.environ["MAYDAY_NUM_GPU"])
        if num_predict:
            opts["num_predict"] = num_predict
        body = {"model": self.name, "messages": messages, "stream": True, "options": opts, "keep_alive": "30m"}
        if tools:
            body["tools"] = tools
        if fmt:
            body["format"] = fmt
        if self._think_ok:
            body["think"] = bool(think)
        self._aborted = False
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        text, thinking, calls, final = [], [], [], {}
        try:
            with self.client.stream("POST", self.url + "/api/chat", json=body,
                                    timeout=self._httpx.Timeout(timeout, connect=min(5.0, timeout))) as r:
                self._resp = r
                if r.status_code >= 400:
                    err = r.read().decode("utf8", "replace")
                    if r.status_code == 400 and "think" in err.lower() and self._think_ok:
                        self._think_ok = False
                        self._resp = None
                        return self._call(messages, tools, think, timeout, on_text, on_think, should_stop, num_predict, fmt)
                    raise ModelError(f"HTTP {r.status_code}: {trunc(err, 160)}")
                for line in r.iter_lines():
                    if self._aborted or (should_stop and should_stop()):
                        raise ModelAborted("aborted")
                    if time.monotonic() > deadline:
                        raise ModelTimeout(f"model call exceeded {timeout:.0f}s")
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    if chunk.get("error"):
                        raise ModelError(trunc(chunk["error"], 160))
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        text.append(msg["content"])
                        if on_text:
                            on_text(msg["content"])
                    if msg.get("thinking"):
                        thinking.append(msg["thinking"])
                        if on_think:
                            on_think(msg["thinking"])
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        calls.append(ToolCall(tc.get("id") or f"call_{len(calls)}", fn.get("name", ""),
                                              self._fix_args(fn.get("arguments", {}))))
                    if chunk.get("done"):
                        final = chunk
        except ModelAborted:
            raise
        except ModelError:
            self.breaker.fail()
            raise
        except self._httpx.TimeoutException as e:
            self.breaker.fail()
            raise ModelTimeout(f"{type(e).__name__}: {e}") from e
        except Exception as e:
            if self._aborted:
                raise ModelAborted("aborted") from e
            self.breaker.fail()
            if isinstance(e, (self._httpx.ConnectError, ConnectionError)):
                self._reach, self._reach_at = False, time.monotonic()
            raise ModelError(f"Gemma unavailable: {e}") from e
        finally:
            self._resp = None
        self.breaker.ok()
        self._reach = True
        stats = {"ms": int((time.monotonic() - t0) * 1000), "prompt_tokens": final.get("prompt_eval_count"),
                 "eval_tokens": final.get("eval_count"), "think": bool(think), "tool_calls": len(calls)}
        self.calls.append(stats)
        log.info("ollama call %s", stats)
        return ModelReply("".join(text).strip(), calls, "".join(thinking).strip(), via=self.name, stats=stats)

    @staticmethod
    def _fix_args(args) -> dict:
        if isinstance(args, str):
            args = parse_json_loose(args)
        if not isinstance(args, dict):
            return {}
        out = {}
        for k, v in args.items():
            if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
                pv = parse_json_loose(v)
                v = pv if pv is not None else v
            out[k] = v
        return out

    @classmethod
    def _salvage(cls, reply: ModelReply, tools) -> None:
        """Models sometimes print the call as text (JSON, a JSON list, or name(args)); recover it."""
        if reply.tool_calls or not reply.text:
            return
        names = {t["function"]["name"] for t in tools or []}

        def add(name, args):
            if name in names:
                reply.tool_calls.append(ToolCall(f"call_salv{len(reply.tool_calls)}", name, cls._fix_args(args or {})))
        obj = parse_json_loose(reply.text)
        items = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) else []
        for o in items:
            if not isinstance(o, dict):
                continue
            fn = o.get("function") if isinstance(o.get("function"), dict) else {}
            name = o.get("name") or o.get("tool") or fn.get("name") or (o.get("function") if isinstance(o.get("function"), str) else None)
            args = o.get("arguments") or o.get("args") or o.get("parameters") or fn.get("arguments")
            add(name, args)
        if reply.tool_calls:
            return
        for line in reply.text.splitlines():
            m = _CALL_RE.match(line.strip().strip("`"))
            if m and m.group(1) in names:
                inner = m.group(2)
                args = parse_json_loose("{" + inner + "}") if ":" in inner and "=" not in inner else None
                if args is None and "=" in inner:
                    args = {k: v.strip("'\" ") for k, v in re.findall(r"(\w+)\s*=\s*([^,]+)", inner)}
                add(m.group(1), args or {})

    @staticmethod
    def _prep_tools(tools):
        if not tools:
            return tools
        from .tools import slim_specs
        names = {t["function"]["name"] for t in tools}
        slim = slim_specs(names)
        return slim or tools

    def chat(self, messages, tools=None, *, think=False, timeout=30.0, on_text=None, require_tool=False,
             on_think=None, should_stop=None, num_predict=None, **kw) -> ModelReply:
        msgs = compact_messages(messages)
        sent_tools = self._prep_tools(tools)
        np_ = num_predict or (1800 if think else 700)
        reply = self._call(msgs, sent_tools, think, timeout, on_text, on_think, should_stop, np_)
        if sent_tools:
            self._salvage(reply, sent_tools)
        if require_tool and sent_tools and not reply.tool_calls:
            m2 = msgs + [{"role": "assistant", "content": trunc(reply.text or "(no tool call)", 300)},
                         {"role": "user", "content": "Respond only with tool call(s) from the provided tools, no prose."}]
            reply2 = self._call(m2, sent_tools, False, timeout, on_text, on_think, should_stop, 700)
            self._salvage(reply2, sent_tools)
            reply2.stats["reasked"] = True
            reply = reply2
        return reply


def make_gateway(prefer: Optional[str] = None, **_ignored) -> "OllamaGateway":
    """Always the Ollama/Gemma gateway (MAYDAY_MODEL_NAME, default gemma4:e4b). `prefer` is accepted for
    backwards compatibility and ignored. If the server is down the status says so and incidents end honestly."""
    g = OllamaGateway()
    g.check()
    return g
