"""Run recording (bus -> JSONL under backend/runs/) and timed replay through the same bus/WebSocket.

File format (one JSON object per line):
  {"kind":"header","run_id","scenario","chaos","seed","severity","fault_offset_s","started","version":1}
  {"kind":"ev","w":<wall seconds since run start>,"env":{<bus envelope>}}
  {"kind":"evidence","id":"ev_...","body":{<Evidence>}}        (resolved at record time so chips work in replay)
Bulletin images referenced by evidence are copied next to the run as <run_id>.<BANK>.png.

Only runs that contained an incident are persisted (the file is created when `incident.opened` fires and the
pre-incident events buffered so far are flushed into it). The newest `keep` runs are retained.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

RUN_ID_RE = re.compile(r"^run_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")
POST_CLOSE_MS = 60_000          # keep recording this long (sim time) after incident.closed
MAX_FILE_EVENTS = 200_000
PRE_BUFFER = 40_000


def _default(o):
    try:
        return o.item()           # numpy scalar
    except Exception:
        return str(o)


def _dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=_default, ensure_ascii=False)


class Recorder:
    def __init__(self, runs_dir, keep: int = 10, clock: Callable[[], float] = time.monotonic,
                 resolver: Optional[Callable[[str], Optional[dict]]] = None,
                 png_resolver: Optional[Callable[[str], Optional[str]]] = None):
        self.dir = Path(runs_dir)
        self.keep = keep
        self.clock = clock
        self.resolver = resolver
        self.png_resolver = png_resolver
        self.paused = False
        self._lock = threading.RLock()
        self._meta: Optional[dict] = None
        self._buf: deque = deque(maxlen=PRE_BUFFER)
        self._t0 = 0.0
        self._f = None
        self._n = 0
        self._closed_at: Optional[int] = None
        self._seen_ev: set = set()
        self._cache: dict = {}
        self.run_id: Optional[str] = None

    # ---------------------------------------------------------------- lifecycle
    def begin(self, scenario: str, seed: int, severity: float, fault_offset_s: float, chaos: bool = False) -> None:
        with self._lock:
            self._finish()
            self._buf.clear()
            self._seen_ev = set()
            self._n = 0
            self._closed_at = None
            self._t0 = self.clock()
            self.run_id = None
            self._meta = {"kind": "header", "scenario": scenario, "chaos": bool(chaos), "seed": int(seed),
                          "severity": float(severity), "fault_offset_s": float(fault_offset_s), "version": 1}

    def close(self) -> None:
        with self._lock:
            self._finish()

    def _finish(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except Exception:
                pass
            self._f = None

    def _open_file(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        self.run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        hdr = dict(self._meta, run_id=self.run_id, started=now.isoformat(timespec="seconds"))
        self._f = open(self.dir / f"{self.run_id}.jsonl", "w", encoding="utf-8")
        self._f.write(_dumps(hdr) + "\n")
        for w, env in self._buf:
            self._write_ev(w, env)
        self._buf.clear()
        self._prune()

    def _write_ev(self, w: float, env: dict) -> None:
        self._f.write(_dumps({"kind": "ev", "w": round(w, 3), "env": env}) + "\n")
        self._n += 1

    def _prune(self) -> None:
        files = sorted(self.dir.glob("run_*.jsonl"), key=lambda p: p.name)
        for p in files[:-self.keep] if len(files) > self.keep else []:
            rid = p.stem
            try:
                p.unlink()
            except OSError:
                pass
            for png in self.dir.glob(f"{rid}.*.png"):
                try:
                    png.unlink()
                except OSError:
                    pass

    # ---------------------------------------------------------------- bus subscriber
    def on_event(self, env: dict) -> None:
        if self.paused:
            return
        try:
            with self._lock:
                if self._meta is None:
                    return
                w = self.clock() - self._t0
                if self._f is None:
                    self._buf.append((w, env))
                    if env["type"] == "incident.opened":
                        self._open_file()
                    return
                if self._n >= MAX_FILE_EVENTS:
                    return
                if env["type"] == "incident.closed":
                    self._closed_at = env["ts"]
                elif self._closed_at is not None and env["ts"] - self._closed_at > POST_CLOSE_MS:
                    return
                self._write_ev(w, env)
                self._capture_evidence(env)
                if self._n % 50 == 0:
                    self._f.flush()
                if env["type"] in ("rca.ready", "incident.closed", "chaos.revealed", "postmortem.ready"):
                    self._f.flush()
        except Exception:
            pass   # recording must never disturb the pipeline

    def _capture_evidence(self, env: dict) -> None:
        if self.resolver is None:
            return
        t, d = env["type"], env["data"]
        ids = []
        if t == "agent.tool_result":
            ids = d.get("evidence_ids") or []
        elif t == "voice.answer":
            ids = d.get("evidence_ids") or []
        elif t == "rca.ready":
            ids = [e.get("id") for e in (d.get("rca") or {}).get("evidence", [])]
        for eid in ids:
            if not eid or eid in self._seen_ev:
                continue
            self._seen_ev.add(eid)
            body = self.resolver(eid)
            if not body:
                continue
            self._f.write(_dumps({"kind": "evidence", "id": eid, "body": body}) + "\n")
            bl = (body.get("body") or {}).get("bulletin") if isinstance(body, dict) else None
            if bl and self.png_resolver:
                m = re.search(r"/api/bulletin/(\w+)\.png", str(bl.get("image_url", "")))
                if m:
                    src = self.png_resolver(m.group(1))
                    if src and Path(src).exists():
                        shutil.copyfile(src, self.dir / f"{self.run_id}.{m.group(1)}.png")

    # ---------------------------------------------------------------- listing / loading
    def list_runs(self) -> list:
        with self._lock:
            if self._f is not None:
                self._f.flush()
        out = []
        if not self.dir.exists():
            return out
        for p in sorted(self.dir.glob("run_*.jsonl"), key=lambda p: p.name, reverse=True)[: self.keep]:
            try:
                st = p.stat()
                key = (st.st_size, st.st_mtime_ns)
                c = self._cache.get(p.name)
                if not c or c[0] != key:
                    c = (key, self._scan(p))
                    self._cache[p.name] = c
                out.append(c[1])
            except Exception:
                continue
        return out

    @staticmethod
    def _scan(p: Path) -> dict:
        hdr, n, last_w, revealed = None, 0, 0.0, False
        with open(p, "rb") as f:
            for i, line in enumerate(f):
                if i == 0:
                    hdr = json.loads(line)
                elif line.startswith(b'{"kind":"ev"'):
                    n += 1
                    m = re.match(rb'\{"kind":"ev","w":([0-9.]+)', line)
                    if m:
                        last_w = float(m.group(1))
                    if not revealed and b'"type":"chaos.revealed"' in line:
                        revealed = True
        hide = bool(hdr.get("chaos")) and not revealed
        return {"run_id": hdr["run_id"], "scenario": None if hide else hdr["scenario"], "chaos": bool(hdr.get("chaos")),
                "started": hdr["started"], "duration_s": round(last_w, 2), "events": n}

    def load(self, run_id: str) -> Optional[dict]:
        if not RUN_ID_RE.match(run_id or ""):
            return None
        p = self.dir / f"{run_id}.jsonl"
        if run_id == self.run_id:
            with self._lock:
                if self._f is not None:
                    self._f.flush()
        if not p.exists():
            return None
        header, events, evidence = None, [], {}
        with open(p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue   # tolerate a torn last line after a crash
                if i == 0:
                    header = o
                elif o.get("kind") == "ev":
                    events.append((float(o["w"]), o["env"]))
                elif o.get("kind") == "evidence":
                    evidence[o["id"]] = o["body"]
        if header is None:
            return None
        pngs = {m.group(1): str(q) for q in self.dir.glob(f"{run_id}.*.png")
                if (m := re.match(rf"{re.escape(run_id)}\.(\w+)\.png$", q.name))}
        return {"header": header, "events": events, "evidence": evidence, "pngs": pngs}


class Replayer:
    """Re-emits a recorded run through the hub's bus with its original timing (scaled by `speed`)."""

    def __init__(self, hub):
        self.hub = hub
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.run_id: Optional[str] = None
        self.speed = 1.0
        self.position_s = 0.0
        self.duration_s = 0.0
        self.playing = False
        self.evidence: dict = {}
        self.pngs: dict = {}

    @property
    def active(self) -> bool:
        return self.run_id is not None

    def info(self) -> Optional[dict]:
        if not self.active:
            return None
        return {"run_id": self.run_id, "playing": self.playing, "speed": self.speed,
                "position_s": round(self.position_s, 2), "duration_s": round(self.duration_s, 2)}

    def start(self, run_id: str, speed: float = 1.0) -> dict:
        hub = self.hub
        rec = hub.recorder.load(run_id)
        if rec is None:
            return {"ok": False, "error": f"unknown run {run_id!r}"}
        self._halt()
        hub.begin_replay(run_id)
        self.run_id, self.speed = run_id, max(0.01, float(speed))
        self.evidence, self.pngs = rec["evidence"], rec["pngs"]
        self.position_s, self.playing = 0.0, True
        evs = [(w, e) for w, e in rec["events"] if e["type"] != "snapshot"]
        self.duration_s = evs[-1][0] if evs else 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._play, args=(evs, self._stop), daemon=True, name="replay")
        self._thread.start()
        return {"ok": True, "run_id": run_id, "events": len(evs), "duration_s": round(self.duration_s, 2)}

    def _halt(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        self.playing = False

    def stop(self, restore: bool = True) -> bool:
        was = self.active
        self._halt()
        self.run_id = None
        self.evidence, self.pngs = {}, {}
        if was:
            self.hub.end_replay(restore)
        return was

    def _play(self, evs: list, stop: threading.Event) -> None:
        hub = self.hub
        t0 = time.monotonic()
        try:
            for w, env in evs:
                delay = w / self.speed - (time.monotonic() - t0)
                if delay > 0 and stop.wait(delay):
                    return
                if stop.is_set():
                    return
                self.position_s = w
                data = env["data"]
                if env["type"] == "sim.status":
                    data = dict(data, running=False, replay=True, replay_run_id=self.run_id)
                hub.bus.emit(env["type"], data, ts=env["ts"])
        finally:
            if not stop.is_set():
                self.playing = False
