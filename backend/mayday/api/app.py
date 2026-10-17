"""FastAPI app: REST + resumable WebSocket wiring the simulator and the agent."""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import random
import threading
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from mayday.api.replay import Recorder, Replayer
from mayday.api.state import StateTracker
from mayday.core.bus import Bus, CONTRACT_VERSION
from mayday.sim.runtime import Simulation

ROOT = Path(__file__).resolve().parents[3]
RUNS_DIR = ROOT / "backend" / "runs"
CHAOS_POOL = ["S1", "S2", "S3", "S5"]
SCENARIOS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")
MAX_AUDIO_S = 30.0
MAX_AUDIO_BYTES = 2 * 1024 * 1024          # non-WAV containers (webm/ogg/mp4 opus: ~0.5 MB for 30 s)
MAX_WAV_BYTES = 6 * 1024 * 1024            # 30 s of 48 kHz stereo 16-bit
MAX_ASK_CHARS = 2000
# events produced by the agent that belong to a specific incident / investigation
_AGENT_SCOPED = ("agent.", "hypothesis.", "verify.", "rca.", "twin.", "action.", "impact.", "comms.",
                 "postmortem.", "incident.")


class ScenarioReq(BaseModel):
    scenario: str
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1)
    severity: Optional[float] = Field(None, ge=0.1, le=5.0)
    fault_offset_s: Optional[float] = Field(None, ge=5.0, le=3600.0)


class ApproveReq(BaseModel):
    incident_id: str
    action_id: str
    token: Optional[str] = None


class AskReq(BaseModel):
    text: str = Field(..., max_length=MAX_ASK_CHARS)
    lang: Optional[str] = Field(None, max_length=12)


class CommsReq(BaseModel):
    incident_id: str


class SpeedReq(BaseModel):
    speed: Optional[float] = Field(None, ge=0.0, le=500.0)
    advance_s: Optional[float] = Field(None, ge=0.0, le=3600.0)
    paused: Optional[bool] = None


class FollowupReq(BaseModel):
    incident_id: str = Field(..., min_length=1, max_length=64)
    kind: Literal["permanent_fix", "escalate_bank", "merchant_notice", "followup_tasks"]


class ModelReq(BaseModel):
    model: str = Field(..., min_length=1, max_length=128)     # an installed Ollama model name


class ReplayStartReq(BaseModel):
    run_id: str = Field(..., max_length=64)
    speed: float = Field(1.0, ge=0.01, le=1000.0)     # 1.0 = original timing


class GatedBus:
    """Bus handed to the orchestrator. Drops incident-scoped agent events that are not valid for the current
    run (a cancelled agent thread from the previous scenario, or any agent event during a replay)."""

    def __init__(self, bus, hub):
        self._bus, self._hub = bus, hub

    def emit(self, type, data, ts=None):
        if type.startswith(_AGENT_SCOPED) and self._hub.gate_closed():
            return -1
        return self._bus.emit(type, data, ts=ts)

    def __getattr__(self, name):
        return getattr(self._bus, name)


class Hub:
    """Owns bus, simulation, tracker, orchestrator, recorder and chaos state."""

    def __init__(self, autostart: bool = True, use_agent: bool = True, seed: int = 1, speed: float = 1.0,
                 orchestrator=None, runs_dir=None, orchestrator_factory=None):
        self.bus = Bus()
        self.sim = Simulation(self.bus, seed=seed)
        self.sim.speed = speed
        self.tracker = StateTracker(self.bus)
        self.ctl = threading.RLock()
        self.autostart = autostart
        self.orch = orchestrator
        self.gateway = None
        self.agent_error: Optional[str] = None
        self.chaos_round = 0
        self.chaos: Optional[dict] = None
        self.replaying = False
        self._was_running: Optional[bool] = None
        self.run_has_incident = False
        self.recorder = Recorder(runs_dir or os.environ.get("MAYDAY_RUNS_DIR") or RUNS_DIR,
                                 resolver=self.evidence, png_resolver=self._bulletin_path)
        self.replayer = Replayer(self)
        if orchestrator_factory is not None:      # tests: inject an Orchestrator wired to this hub's world/bus
            self.orch = orchestrator_factory(self.sim.world, GatedBus(self.bus, self))
            self.gateway = getattr(self.orch, "gateway", None)
        elif self.orch is None and use_agent:
            self._load_agent()
        self._set_model()
        self.bus.subscribe(self.recorder.on_event)      # before _on_event: incident.opened precedes agent events
        self.bus.subscribe(self._on_event)
        p = self.sim.engine.p
        self.recorder.begin(p.scenario, p.seed, p.severity, p.fault_offset_s)
        self._reset_tracker()
        self.sim.emit_status()

    # ---- agent wiring (try-import)
    def _load_agent(self):
        try:
            from mayday.agent.orchestrator import Orchestrator
            from mayday.agent.gateway import make_gateway
            self.gateway = make_gateway()
            self.orch = Orchestrator(self.sim.world, GatedBus(self.bus, self), self.gateway,
                                     model_timeout_s=float(os.environ.get("MAYDAY_MODEL_TIMEOUT_S", "30")),
                                     total_budget_s=float(os.environ.get("MAYDAY_TOTAL_BUDGET_S", "90")))
            if hasattr(self.gateway, "warm_up"):
                threading.Thread(target=lambda: self.gateway.warm_up(), daemon=True).start()
        except Exception as e:  # run without agent
            self.orch = None
            self.agent_error = f"{type(e).__name__}: {e}"

    def _set_model(self):
        st = {"backend": "scripted", "name": "none (agent not loaded)", "healthy": False, "warm": False}
        try:
            g = getattr(self.orch, "gateway", None) or self.gateway   # orchestrator's gateway wins (set_mode swaps it)
            if g is not None:
                st = g.status()
        except Exception:
            pass
        self.tracker.model = st

    def _reset_tracker(self):
        self.tracker.clear_run()
        for d in self.sim.engine.deploys:
            if d["ts"] <= self.sim.engine.now:
                self.tracker.deploys.append(d)

    def installed_models(self):
        """Installed Ollama models if the agent/gateway can list them, else None."""
        for o in (self.orch, getattr(self.orch, "gateway", None), self.gateway):
            for name in ("list_models", "installed_models"):
                fn = getattr(o, name, None)
                if callable(fn):
                    try:
                        return list(fn())
                    except Exception:
                        return None
        return None

    def gate_closed(self) -> bool:
        return self.replaying or not self.run_has_incident

    # ---- events
    def _on_event(self, env: dict):
        if self.replaying:
            return
        t, d = env["type"], env["data"]
        if t == "incident.opened":
            self.run_has_incident = True
            if self.orch is not None:
                threading.Thread(target=self._run_agent, args=(d["incident"],), daemon=True).start()
        elif self.chaos and not self.chaos.get("revealed"):
            if t == "rca.ready":
                rca = d["rca"]
                cause = "inconclusive" if rca.get("inconclusive") else rca.get("cause", "")
                self._reveal(cause)
            elif t == "agent.state" and d.get("state") in ("inconclusive", "error"):
                self._reveal("inconclusive")

    def _run_agent(self, incident):
        try:
            self.orch.on_incident(incident)
        except Exception as e:
            self.bus.emit("error", {"code": "agent_failure", "message": str(e), "recoverable": True})

    def _reveal(self, agent_cause: str):
        c = self.chaos
        if not c or c.get("revealed"):
            return
        c["revealed"] = True
        gt = self.sim.world.ground_truth() or {}
        truth = gt.get("cause")
        causes = set(gt.get("causes") or [truth])
        correct = agent_cause in causes or (agent_cause and agent_cause == truth)
        self.sim.chaos_hidden = False
        self.sim.emit_status()
        self.bus.emit("chaos.revealed", {"round": c["round"], "scenario": gt.get("scenario"),
                                         "ground_truth": truth or "", "agent_cause": agent_cause,
                                         "correct": bool(correct)})

    # ---- control
    def start_scenario(self, scenario, seed=None, severity=None, fault_offset_s=None, chaos_hidden=False):
        with self.ctl:
            if self.replayer.active:
                self.replayer.stop(restore=False)
            self.run_has_incident = False          # closes the gate before the old agent run is cancelled
            if self.orch is not None and hasattr(self.orch, "reset"):
                try:
                    self.orch.reset()
                except Exception:
                    pass
            p = self.sim.engine.p
            self.recorder.begin(scenario,
                                p.seed if seed is None else int(seed),
                                p.severity if severity is None else float(severity),
                                p.fault_offset_s if fault_offset_s is None else float(fault_offset_s),
                                chaos=chaos_hidden)
            self.sim.reset(scenario, seed, severity, fault_offset_s, chaos_hidden)
            self.bus.reset()
            self._reset_tracker()
            self.sim.emit_status()
            self.bus.emit("snapshot", self.tracker.snapshot())
            if not chaos_hidden:
                self.chaos = None

    def arm_chaos(self):
        with self.ctl:
            self.chaos_round += 1
            rnd = random.SystemRandom()
            sc = rnd.choice(CHAOS_POOL)
            self.start_scenario(sc, seed=rnd.randint(1, 10**6), severity=round(rnd.uniform(0.9, 1.4), 2),
                                fault_offset_s=rnd.randint(10, 40), chaos_hidden=True)
            self.chaos = {"round": self.chaos_round, "revealed": False}
            self.bus.emit("chaos.armed", {"round": self.chaos_round})
            return self.chaos_round

    # ---- replay (driven by api.replay.Replayer)
    def begin_replay(self, run_id: str):
        with self.ctl:
            if not self.replaying:
                self._was_running = self.sim.running
            self.replaying = True
            self.tracker.replaying = True
            self.recorder.paused = True
            self.sim.running = False
            with self.sim.lock:                      # wait for an in-flight live advance to finish
                pass
            if self.orch is not None and hasattr(self.orch, "reset"):
                try:
                    self.orch.reset()
                except Exception:
                    pass
            self.chaos = None
            self.sim.chaos_hidden = False
            self.sim.replay_run_id = run_id
            self.bus.reset()
            self.tracker.clear_run()
            self.bus.emit("sim.status", {"scenario": None, "running": False, "sim_ms": int(self.sim.engine.now),
                                         "speed": self.sim.speed, "fault_active": False, "chaos_hidden": False,
                                         "replay": True, "replay_run_id": run_id})
            self.bus.emit("snapshot", self.tracker.snapshot())

    def end_replay(self, restore: bool = True):
        with self.ctl:
            self.replaying = False
            self.tracker.replaying = False
            self.recorder.paused = False
            self.sim.replay_run_id = None
            self.sim.running = bool(self._was_running)
            self._was_running = None
            if restore:
                self.start_scenario("S0")

    def stop_replay(self) -> bool:
        with self.ctl:
            return self.replayer.stop(restore=True)

    def shutdown(self):
        try:
            self.replayer.stop(restore=False)
        except Exception:
            pass
        self.sim.stop()
        self.recorder.close()

    # ---- lookups
    def _bulletin_path(self, bank: str) -> Optional[str]:
        b = self.sim.telemetry.read_bulletin(bank)
        return b["png_path"] if b else None

    def evidence(self, eid: str):
        if self.replayer.active:
            e = self.replayer.evidence.get(eid)
            if e:
                return e
        if self.orch is not None:
            try:
                e = self.orch.get_evidence(eid)
                if e:
                    return e
            except Exception:
                pass
        if self.replaying:
            return None
        tel = self.sim.telemetry
        if eid.startswith("ev_deploy_"):
            d = tel.get_deploy(eid.split("_")[-1])
            if d:
                return {"id": eid, "kind": "deploy", "title": f"Deploy #{d['number']} ({d['service']})", "ts": d["ts"], "body": d}
        if eid.startswith("ev_bulletin_"):
            b = tel.read_bulletin(eid.split("_", 2)[-1].upper() if "BANK" in eid.upper() else "BANK_X")
            if b:
                return {"id": eid, "kind": "bulletin", "title": "Bank status bulletin", "ts": b["published_ts"],
                        "body": {"bulletin": {"image_url": b["image_url"], "extracted": b["extracted"],
                                              "consistent": True, "detail": b["headline"]}}}
        return None


def _model_status(hub: Hub) -> dict:
    hub._set_model()
    return hub.tracker.model


async def _maybe_await(x):
    if inspect.isawaitable(x):
        return await x
    return x


def _audio_seconds(data: bytes) -> Optional[float]:
    """Exact duration for WAV; None for other containers (size-capped instead)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    return None


def build_app(autostart: bool = None, use_agent: bool = True, orchestrator=None, speed: float = 1.0,
              runs_dir=None, orchestrator_factory=None) -> FastAPI:
    if autostart is None:
        autostart = os.environ.get("MAYDAY_AUTOSTART", "1") != "0"
    hub = Hub(autostart=autostart, use_agent=use_agent, speed=speed, orchestrator=orchestrator, runs_dir=runs_dir,
              orchestrator_factory=orchestrator_factory)

    @asynccontextmanager
    async def lifespan(app):
        if hub.autostart:
            hub.sim.start()
        yield
        hub.shutdown()

    app = FastAPI(title="ProofLine", lifespan=lifespan)
    app.state.hub = hub
    allowed_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    frontend_origins = os.environ.get("MAYDAY_ALLOWED_ORIGINS", "https://frontend-zeta-olive-60.vercel.app")
    allowed_origins += [origin.strip().rstrip("/") for origin in frontend_origins.split(",") if origin.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=list(dict.fromkeys(allowed_origins)),
                       allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

    def err(msg, code=400):
        return JSONResponse({"ok": False, "error": msg}, status_code=code)

    @app.exception_handler(Exception)
    async def _any(request: Request, exc: Exception):
        hub.bus.emit("error", {"code": "api_error", "message": str(exc), "recoverable": True})
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors()[:3]:
            loc = ".".join(str(x) for x in e.get("loc", ()) if x not in ("body", "query"))
            msgs.append(f"{loc}: {e.get('msg', 'invalid')}" if loc else str(e.get("msg", "invalid")))
        return JSONResponse({"ok": False, "error": "invalid request: " + "; ".join(msgs)}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)

    @app.get("/api/health")
    def health():
        out = {"ok": True, "model": _model_status(hub), "contract_version": CONTRACT_VERSION,
               "agent_loaded": hub.orch is not None, "agent_error": hub.agent_error,
               "replay": hub.replayer.info()}
        models = hub.installed_models()
        if models is not None:
            out["models"] = models
        return out

    @app.get("/api/state")
    def state():
        return hub.tracker.snapshot()

    @app.get("/api/evidence/{eid}")
    def evidence(eid: str):
        e = hub.evidence(eid)
        if not e:
            return err("evidence not found", 404)
        return e

    @app.get("/api/bulletin/{bank}.png")
    def bulletin(bank: str):
        if hub.replayer.active:
            p = hub.replayer.pngs.get(bank)
            if p and Path(p).exists():
                return FileResponse(p, media_type="image/png")
            return err("no bulletin", 404)
        b = hub.sim.telemetry.read_bulletin(bank)
        if not b or not Path(b["png_path"]).exists():
            return err("no bulletin", 404)
        return FileResponse(b["png_path"], media_type="image/png")

    @app.post("/api/scenario")
    def scenario(r: ScenarioReq):
        if r.scenario not in SCENARIOS:
            return err("unknown scenario")
        hub.start_scenario(r.scenario, r.seed, r.severity, r.fault_offset_s)
        return {"ok": True}

    @app.post("/api/reset")
    def reset():
        hub.start_scenario("S0")
        return {"ok": True}

    @app.post("/api/approve")
    async def approve(r: ApproveReq):
        if hub.orch is None:
            return err("agent unavailable", 503)
        res = await run_in_threadpool(hub.orch.approve, r.incident_id, r.action_id, r.token or "")
        return res if "ok" in res else {"ok": True, **res}

    @app.post("/api/reject")
    async def reject(r: ApproveReq):
        if hub.orch is None:
            return err("agent unavailable", 503)
        res = await run_in_threadpool(hub.orch.reject, r.incident_id, r.action_id)
        return res if "ok" in res else {"ok": True, **res}

    @app.post("/api/chaos")
    def chaos():
        return {"ok": True, "round": hub.arm_chaos()}

    @app.post("/api/model")
    async def set_model(r: ModelReq):
        fn = getattr(hub.orch, "set_model", None) if hub.orch is not None else None
        if fn is None:
            return err("model switching is not supported by this agent build", 501)
        try:
            st = await run_in_threadpool(fn, r.model.strip())
        except ValueError as e:
            return err(f"invalid model: {e}", 400)
        except Exception as e:
            hub.bus.emit("error", {"code": "model_switch_failed", "message": str(e), "recoverable": True})
            return err(f"model switch failed: {e}", 502)
        if isinstance(st, dict):
            if st.get("ok") is False:
                return err(str(st.get("error") or "model switch failed"), 400)
            hub.tracker.model = {k: v for k, v in st.items() if k != "ok"}
        else:
            hub._set_model()
        hub.gateway = getattr(hub.orch, "gateway", hub.gateway)
        return {"ok": True, "model": hub.tracker.model}

    @app.post("/api/ask")
    async def ask(r: AskReq):
        text = r.text.strip()
        if not text:
            return err("text is empty")
        if hub.orch is None:
            return err("agent unavailable", 503)
        if hub.replaying:
            return err("replay active; stop the replay to ask live questions", 409)
        try:
            qid = await run_in_threadpool(hub.orch.ask, text, r.lang)
        except Exception as e:
            hub.bus.emit("error", {"code": "ask_failed", "message": str(e), "recoverable": True})
            return err(f"ask failed: {e}", 503)
        return {"ok": True, "query_id": qid}

    @app.post("/api/voice")
    async def voice(audio: UploadFile = File(...), duration_s: Optional[float] = Form(None)):
        if hub.orch is None or not hasattr(hub.orch, "ask_audio"):
            return err("voice unavailable; use /api/ask", 503)
        if hub.replaying:
            return err("replay active; stop the replay to ask live questions", 409)
        if duration_s is not None and duration_s > MAX_AUDIO_S:
            return err(f"audio too long ({duration_s:.0f}s); maximum is {MAX_AUDIO_S:.0f}s", 413)
        data = await audio.read(MAX_WAV_BYTES + 1)
        if not data:
            return err("empty audio upload")
        try:
            secs = _audio_seconds(data)
        except Exception:
            return err("unreadable WAV audio")
        if secs is not None and secs > MAX_AUDIO_S + 0.05:
            return err(f"audio too long ({secs:.0f}s); maximum is {MAX_AUDIO_S:.0f}s", 413)
        if secs is None and len(data) > MAX_AUDIO_BYTES:
            return err(f"audio too large for {MAX_AUDIO_S:.0f}s of speech ({len(data) // 1024} KB)", 413)
        if len(data) > MAX_WAV_BYTES:
            return err("audio too large", 413)
        seq0 = hub.bus.seq
        try:
            qid = await run_in_threadpool(hub.orch.ask_audio, data, audio.content_type or "audio/webm")
        except Exception as e:
            hub.bus.emit("error", {"code": "audio_failed", "message": str(e), "recoverable": True})
            return err(f"voice failed: {e}; use /api/ask", 503)
        out = {"ok": True, "query_id": qid}
        if any(e["type"] == "error" and e["data"].get("code") == "audio_unsupported" for e in hub.bus.since(seq0)):
            out["fallback"] = "audio_unsupported"
            out["hint"] = "This model cannot take audio. Use browser speech-to-text and POST /api/ask."
        return out

    @app.post("/api/followup")
    async def followup(r: FollowupReq):
        fn = getattr(hub.orch, "followup", None) if hub.orch is not None else None
        if fn is None:
            return err("follow-up drafting is not supported by this agent build", 501)
        if hub.replaying:
            return err("replay active; stop the replay first", 409)
        cur = (hub.tracker.incident or {}).get("id")
        if cur != r.incident_id:
            return err(f"unknown incident {r.incident_id!r}", 404)
        try:
            res = await run_in_threadpool(fn, r.incident_id, r.kind)
        except ValueError as e:
            return err(str(e), 400)
        except Exception as e:
            hub.bus.emit("error", {"code": "followup_failed", "message": str(e), "recoverable": True})
            return err(f"follow-up failed: {e}", 502)
        if isinstance(res, dict) and res.get("ok") is False:
            return err(str(res.get("error") or "follow-up failed"), 400)
        return {"ok": True}

    @app.post("/api/comms/approve")
    def comms_approve(r: CommsReq):
        return {"ok": True}

    @app.get("/api/eval/latest")
    def eval_latest():
        p = ROOT / "eval" / "results" / "latest.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"generated": "", "model": "none", "total_runs": 0, "false_incident_rate": 0.0,
                    "rows": [], "ablations": []}

    @app.get("/api/replay/list")
    def replay_list():
        return {"ok": True, "runs": hub.recorder.list_runs(), "active": hub.replayer.info()}

    @app.post("/api/replay/start")
    def replay_start(r: ReplayStartReq):
        with hub.ctl:
            res = hub.replayer.start(r.run_id, r.speed)
        return res if res.get("ok") else JSONResponse(res, status_code=404)

    @app.post("/api/replay/stop")
    def replay_stop():
        return {"ok": True, "was_active": hub.stop_replay()}

    @app.post("/api/sim/control")
    def sim_control(r: SpeedReq):
        if hub.replaying:
            return err("replay active; simulation controls are ignored until the replay is stopped", 409)
        if r.speed is not None:
            hub.sim.set_speed(r.speed)
        if r.paused is not None:
            hub.sim.pause(r.paused)
        if r.advance_s:
            hub.sim.advance(int(r.advance_s * 1000))
        return {"ok": True, "status": hub.sim.status()}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, since: Optional[int] = None):
        await websocket.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        overflow = []

        def put(env):
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                overflow.append(1)

        unsub = hub.bus.subscribe(lambda env: loop.call_soon_threadsafe(put, env))
        try:
            head = hub.bus.seq
            snap = {"v": 2, "seq": head, "ts": hub.sim.engine.now, "type": "snapshot", "data": hub.tracker.snapshot()}
            await websocket.send_json(snap)
            if since is not None:
                for env in hub.bus.since(since):
                    if env["seq"] <= head:
                        await websocket.send_json(env)
            while True:
                env = await q.get()
                if overflow:
                    await websocket.close(code=1013)
                    return
                if env["seq"] <= head:
                    continue
                await websocket.send_json(env)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsub()

    return app


app = build_app()
