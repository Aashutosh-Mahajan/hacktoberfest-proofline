"""FastAPI app: REST + resumable WebSocket wiring the simulator and the agent."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from mayday.api.state import StateTracker
from mayday.core.bus import Bus, CONTRACT_VERSION
from mayday.sim.runtime import Simulation

ROOT = Path(__file__).resolve().parents[3]
CHAOS_POOL = ["S1", "S2", "S3", "S5"]


class ScenarioReq(BaseModel):
    scenario: str
    seed: Optional[int] = None
    severity: Optional[float] = None
    fault_offset_s: Optional[float] = None


class ApproveReq(BaseModel):
    incident_id: str
    action_id: str
    token: Optional[str] = None


class AskReq(BaseModel):
    text: str
    lang: Optional[str] = None


class CommsReq(BaseModel):
    incident_id: str


class SpeedReq(BaseModel):
    speed: Optional[float] = None
    advance_s: Optional[float] = None
    paused: Optional[bool] = None


class Hub:
    """Owns bus, simulation, tracker, orchestrator and chaos state."""

    def __init__(self, autostart: bool = True, use_agent: bool = True, seed: int = 1, speed: float = 1.0,
                 orchestrator=None):
        self.bus = Bus()
        self.sim = Simulation(self.bus, seed=seed)
        self.sim.speed = speed
        self.tracker = StateTracker(self.bus)
        self.autostart = autostart
        self.orch = orchestrator
        self.gateway = None
        self.agent_error: Optional[str] = None
        self.chaos_round = 0
        self.chaos: Optional[dict] = None
        if self.orch is None and use_agent:
            self._load_agent()
        self._set_model()
        self.bus.subscribe(self._on_event)
        self._reset_tracker()
        self.sim.emit_status()

    # ---- agent wiring (try-import)
    def _load_agent(self):
        try:
            from mayday.agent.orchestrator import Orchestrator
            from mayday.agent.gateway import make_gateway
            self.gateway = make_gateway()
            import os, threading
            self.orch = Orchestrator(self.sim.world, self.bus, self.gateway,
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
            g = self.gateway or getattr(self.orch, "gateway", None)
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

    # ---- events
    def _on_event(self, env: dict):
        t, d = env["type"], env["data"]
        if t == "incident.opened" and self.orch is not None:
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
        if self.orch is not None and hasattr(self.orch, "reset"):
            try:
                self.orch.reset()
            except Exception:
                pass
        self.sim.reset(scenario, seed, severity, fault_offset_s, chaos_hidden)
        self.bus.reset()
        self._reset_tracker()
        self.sim.emit_status()
        self.bus.emit("snapshot", self.tracker.snapshot())
        if not chaos_hidden:
            self.chaos = None

    def arm_chaos(self):
        self.chaos_round += 1
        rnd = random.SystemRandom()
        sc = rnd.choice(CHAOS_POOL)
        self.start_scenario(sc, seed=rnd.randint(1, 10**6), severity=round(rnd.uniform(0.9, 1.4), 2),
                            fault_offset_s=rnd.randint(10, 40), chaos_hidden=True)
        self.chaos = {"round": self.chaos_round, "revealed": False}
        self.bus.emit("chaos.armed", {"round": self.chaos_round})
        return self.chaos_round

    def evidence(self, eid: str):
        if self.orch is not None:
            try:
                e = self.orch.get_evidence(eid)
                if e:
                    return e
            except Exception:
                pass
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


def build_app(autostart: bool = None, use_agent: bool = True, orchestrator=None, speed: float = 1.0) -> FastAPI:
    if autostart is None:
        autostart = os.environ.get("MAYDAY_AUTOSTART", "1") != "0"
    hub = Hub(autostart=autostart, use_agent=use_agent, speed=speed, orchestrator=orchestrator)

    @asynccontextmanager
    async def lifespan(app):
        if hub.autostart:
            hub.sim.start()
        yield
        hub.sim.stop()

    app = FastAPI(title="Proofline", lifespan=lifespan)
    app.state.hub = hub
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

    def err(msg, code=400):
        return JSONResponse({"ok": False, "error": msg}, status_code=code)

    @app.exception_handler(Exception)
    async def _any(request: Request, exc: Exception):
        hub.bus.emit("error", {"code": "api_error", "message": str(exc), "recoverable": True})
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"ok": True, "model": _model_status(hub), "contract_version": CONTRACT_VERSION,
                "agent_loaded": hub.orch is not None, "agent_error": hub.agent_error}

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
        b = hub.sim.telemetry.read_bulletin(bank)
        if not b:
            return err("no bulletin", 404)
        return FileResponse(b["png_path"], media_type="image/png")

    @app.post("/api/scenario")
    def scenario(r: ScenarioReq):
        if r.scenario not in ("S0", "S1", "S2", "S3", "S4", "S5", "S6"):
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

    @app.post("/api/ask")
    async def ask(r: AskReq):
        if hub.orch is None:
            return err("agent unavailable", 503)
        try:
            qid = await run_in_threadpool(hub.orch.ask, r.text, r.lang)
        except Exception as e:
            hub.bus.emit("error", {"code": "ask_failed", "message": str(e), "recoverable": True})
            return err(str(e), 500)
        return {"ok": True, "query_id": qid}

    @app.post("/api/voice")
    async def voice(audio: UploadFile = File(...)):
        if hub.orch is None or not hasattr(hub.orch, "ask_audio"):
            return err("voice unavailable; use /api/ask", 503)
        data = await audio.read()
        qid = await run_in_threadpool(hub.orch.ask_audio, data, audio.content_type or "audio/webm")
        return {"ok": True, "query_id": qid}

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
        return {"ok": True, "runs": []}

    @app.post("/api/replay/start")
    def replay_start():
        return err("replay not implemented", 501)

    @app.post("/api/replay/stop")
    def replay_stop():
        return {"ok": True}

    @app.post("/api/sim/control")
    def sim_control(r: SpeedReq):
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
