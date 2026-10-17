"""Model gateways. Same interface for OllamaGateway (Gemma 4) and ScriptedGateway (offline, deterministic)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .util import parse_json_loose


class ModelError(Exception):
    pass


class ModelTimeout(ModelError):
    pass


class ModelUnavailable(ModelError):
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


class Gateway:
    backend = "scripted"
    name = "base"

    def chat(self, messages: list, tools: Optional[list] = None, *, think: bool = False, timeout: float = 30.0,
             on_text: Optional[Callable[[str], None]] = None, require_tool: bool = False) -> ModelReply:
        raise NotImplementedError

    def status(self) -> dict:
        return {"backend": self.backend, "name": self.name, "healthy": True, "warm": True}

    def warm_up(self) -> dict:
        return self.status()

    def supports_audio(self) -> bool:
        return False

    def transcribe(self, audio: bytes, mime: str) -> dict:
        raise ModelError("audio input not supported by this gateway; use browser speech-to-text and POST /api/ask")

    def extract_bulletin(self, info: dict, bank: str) -> dict:
        ex = info.get("extracted")
        if not ex:
            raise ModelError("no bulletin metadata available")
        return ex


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

    def health(self) -> bool:
        try:
            r = self.client.get(self.url + "/api/tags", timeout=2.0)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n == self.name or n.split(":")[0] == self.name.split(":")[0] for n in names)
        except Exception:
            return False

    def status(self) -> dict:
        return {"backend": "ollama", "name": self.name, "healthy": not self.breaker.open, "warm": self.warm}

    def warm_up(self) -> dict:
        try:
            self._call([{"role": "user", "content": "ping"}], None, False, 240.0, None)
            self.warm = True
        except ModelError:
            self.warm = False
        return self.status()

    def _call(self, messages, tools, think, timeout, on_text) -> ModelReply:
        if not self.breaker.allow():
            raise ModelUnavailable("circuit breaker open")
        body = {"model": self.name, "messages": messages, "stream": True,
                "options": {"temperature": 0.2, "num_ctx": int(os.environ.get("MAYDAY_NUM_CTX", "8192")), **({"num_gpu": int(os.environ["MAYDAY_NUM_GPU"])} if os.environ.get("MAYDAY_NUM_GPU") else {})}, "keep_alive": "30m"}
        if tools:
            body["tools"] = tools
        if think:
            body["think"] = True
        deadline = time.monotonic() + timeout
        text, thinking, calls = [], [], []
        try:
            with self.client.stream("POST", self.url + "/api/chat", json=body,
                                    timeout=self._httpx.Timeout(timeout, connect=min(5.0, timeout))) as r:
                if r.status_code >= 400:
                    raise ModelError(f"HTTP {r.status_code}")
                for line in r.iter_lines():
                    if time.monotonic() > deadline:
                        raise ModelTimeout(f"model call exceeded {timeout:.0f}s")
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        text.append(msg["content"])
                        if on_text:
                            on_text(msg["content"])
                    if msg.get("thinking"):
                        thinking.append(msg["thinking"])
                    for i, tc in enumerate(msg.get("tool_calls") or []):
                        fn = tc.get("function") or {}
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            args = parse_json_loose(args) or {}
                        calls.append(ToolCall(tc.get("id") or f"call_{len(calls)}", fn.get("name", ""), args))
        except ModelError:
            self.breaker.fail()
            raise
        except self._httpx.TimeoutException as e:
            self.breaker.fail()
            raise ModelTimeout(str(e)) from e
        except Exception as e:
            self.breaker.fail()
            raise ModelError(str(e)) from e
        self.breaker.ok()
        return ModelReply("".join(text).strip(), calls, "".join(thinking).strip())

    @staticmethod
    def _salvage(reply: ModelReply, tools) -> None:
        """Models sometimes print the call as JSON text; recover it."""
        if reply.tool_calls or not reply.text:
            return
        obj = parse_json_loose(reply.text)
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or (obj.get("function") or {}).get("arguments")
            names = {t["function"]["name"] for t in tools or []}
            if name in names:
                if isinstance(args, str):
                    args = parse_json_loose(args)
                reply.tool_calls.append(ToolCall("call_salv", name, args or {}))

    def chat(self, messages, tools=None, *, think=False, timeout=30.0, on_text=None, require_tool=False) -> ModelReply:
        reply = self._call(messages, tools, think, timeout, on_text)
        if tools:
            self._salvage(reply, tools)
        if require_tool and tools and not reply.tool_calls:
            msgs = messages + [{"role": "assistant", "content": reply.text or "(no tool call)"},
                               {"role": "user", "content": "Respond with exactly one tool call from the provided tools, no prose."}]
            reply = self._call(msgs, tools, False, timeout, on_text)
            self._salvage(reply, tools)
        return reply


def make_gateway(prefer: Optional[str] = None, **scripted_kwargs) -> Gateway:
    """OllamaGateway if the server is reachable (and serves the model), else ScriptedGateway."""
    prefer = prefer or os.environ.get("MAYDAY_GATEWAY")
    if prefer != "scripted":
        try:
            g = OllamaGateway()
            if g.health():
                return g
        except Exception:
            pass
    from .scripted import ScriptedGateway
    return ScriptedGateway(**scripted_kwargs)


def __getattr__(name):  # lazy re-export to avoid circular import
    if name == "ScriptedGateway":
        from .scripted import ScriptedGateway
        return ScriptedGateway
    raise AttributeError(name)
