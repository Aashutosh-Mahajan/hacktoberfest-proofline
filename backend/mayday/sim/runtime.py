"""Simulation runtime: engine + detector + bus emission + real-time driver thread."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from mayday.core.protocols import BANKS
from mayday.sim.detector import Detector
from mayday.sim.engine import Engine, TICK, T0, L0
from mayday.sim.telemetry import SimTelemetry, p95_from_hist
from mayday.sim.world import SimWorld


class Simulation:
    def __init__(self, bus, seed=1, scenario="S0", fault_offset_s=15.0, severity=1.0, emit_metrics=True):
        self.bus = bus
        self.emit_metrics = emit_metrics
        self.speed = 1.0
        self.running = False
        self.chaos_hidden = False
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self.recent = deque(maxlen=480)
        self.world = None
        self._build(seed, scenario, fault_offset_s, severity)

    def _build(self, seed, scenario, fault_offset_s, severity):
        self.engine = Engine(seed, scenario, fault_offset_s, severity)
        if getattr(self, "world", None) is None:   # keep world/telemetry objects stable for the agent
            self.telemetry = SimTelemetry(self.engine)
            self.world = SimWorld(self.engine, runtime=self)
        else:
            self.telemetry.e = self.engine
            self.world.engine = self.engine
            self.world._pcache = None
            self.world.telemetry.e = self.engine   # orchestrator holds this exact object
        self.detector = Detector(self.engine, self.telemetry)
        self.recent.clear()
        self._deploy_i = sum(1 for d in self.engine.deploys if d["ts"] <= self.engine.now)
        self._last_status = 0
        self.bus.set_clock(lambda: self.engine.now)

    # ------------------------------------------------------------ control
    def reset(self, scenario="S0", seed=None, severity=None, fault_offset_s=None, chaos_hidden=False):
        with self.lock:
            p = self.engine.p
            self._build(p.seed if seed is None else int(seed), scenario,
                        p.fault_offset_s if fault_offset_s is None else float(fault_offset_s),
                        p.severity if severity is None else float(severity))
            self.chaos_hidden = chaos_hidden
        self.emit_status()

    def status(self) -> dict:
        p = self.engine.p
        e = self.engine
        return {"scenario": None if self.chaos_hidden else (p.scenario if p.scenario != "S0" else None),
                "running": self.running, "sim_ms": int(e.now), "speed": self.speed,
                "fault_active": (not self.chaos_hidden) and p.scenario != "S0" and e.now >= e.f0,
                "chaos_hidden": self.chaos_hidden}

    def emit_status(self):
        self.bus.emit("sim.status", self.status(), ts=self.engine.now)

    # ------------------------------------------------------------ metrics
    def _metrics(self, k1: int) -> dict:
        e = self.engine
        a = e.sum_range(k1 - 20, k1)
        t = a.sum(0)
        tot = max(int(t[0]), 1)
        biz = int(t[5:8].sum())
        d = e.sum_range(k1 - 40, k1).sum(0)
        dt = float(d[2:5].sum())
        db = float(d[5:8].sum())
        mix_t = 100 * dt / (dt + db) if dt + db > 0 else 0.0
        k0, _, pz, _ = e.range_arrays(k1 - 20, k1)
        by = {}
        for i, b in enumerate(BANKS):
            r = a[i]
            by[b] = {"success_rate": round(100.0 * r[1] / max(int(r[0]), 1), 2) if r[0] else round(e.ref_success, 2),
                     "p95_ms": round(p95_from_hist(r[8:]), 1), "tps": round(r[0] / 5.0, 1)}
        return {"success_rate": round(100.0 * t[1] / tot, 2),
                "technical_success_rate": round(100.0 * t[1] / max(tot - biz, 1), 2),
                "decline_mix": {"technical_pct": round(mix_t, 1), "business_pct": round(100 - mix_t, 1) if dt + db > 0 else 0.0},
                "tps": round(t[0] / 5.0, 1), "p95_ms": round(p95_from_hist(t[8:]), 1), "by_bank": by,
                "paise_failed_per_min": int(pz[:, :, 0].sum() * 12)}

    # ------------------------------------------------------------ stepping
    def advance(self, ms: int) -> None:
        """Advance sim time, emitting events. Safe to call from any thread."""
        with self.lock:
            nticks = int(round(ms / TICK))
            while nticks > 0:
                n = min(4, nticks)
                self.engine.step(n)
                nticks -= n
                e = self.engine
                if self.emit_metrics:
                    for j in range(n):
                        k1 = e.tick - n + j + 1
                        if k1 - 20 < e.lo:
                            continue
                        m = self._metrics(k1)
                        ts = int(T0 + k1 * TICK)
                        self.recent.append({"ts": ts, "m": m})
                        self.bus.emit("metrics.tick", m, ts=ts)
                while self._deploy_i < len(e.deploys) and e.deploys[self._deploy_i]["ts"] <= e.now:
                    d = e.deploys[self._deploy_i]
                    self._deploy_i += 1
                    self.bus.emit("deploy.event", dict(d), ts=d["ts"])
                for typ, data, ts in self.detector.observe():
                    self.bus.emit(typ, data, ts=ts)
                if e.now - self._last_status >= 5000:
                    self._last_status = e.now
                    self.emit_status()

    # ------------------------------------------------------------ realtime
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-runner")
        self._thread.start()

    def stop(self):
        self._stop = True
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)

    def set_speed(self, speed: float):
        self.speed = max(0.0, float(speed))
        self.emit_status()

    def pause(self, paused: bool = True):
        self.running = not paused
        self.emit_status()

    def _loop(self):
        last = time.perf_counter()
        budget = 0.0
        while not self._stop:
            time.sleep(0.05)
            now = time.perf_counter()
            dt = now - last
            last = now
            if not self.running:
                continue
            budget += dt * 1000.0 * self.speed
            n = int(budget // 1000) * 1000 if budget >= 1000 else int(budget // TICK) * TICK
            n = min(n, 60_000)
            if n >= TICK:
                budget -= n
                try:
                    self.advance(n)
                except Exception as ex:  # never crash the sim thread
                    self.bus.emit("error", {"code": "sim_error", "message": str(ex), "recoverable": True})
