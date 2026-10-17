"""World implementation: control surface over an Engine (fork, preview, actions)."""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from mayday.sim.engine import Engine, TICK
from mayday.sim.telemetry import SimTelemetry

TOL_PP = 1.5


def _trailing(e: Engine, k_from: int, nsteps: int, window_ticks: int = 20, step_ticks: int = 4):
    """Success%/TSR% trailing-5s series sampled every second for ticks starting at k_from."""
    out_s, out_t, ts = [], [], []
    for j in range(1, nsteps + 1):
        k1 = k_from + j * step_ticks
        a = e.sum_range(k1 - window_ticks, k1).sum(0)
        tot = max(int(a[0]), 1)
        out_s.append(100.0 * a[1] / tot)
        out_t.append(100.0 * a[1] / max(tot - int(a[5:8].sum()), 1))
        ts.append(int(e.now - (e.tick - k1) * TICK))
    return ts, out_s, out_t


def first_recovery(ts, vals, target, start_ts, need=3):
    run = 0
    for i, v in enumerate(vals):
        run = run + 1 if v >= target else 0
        if run >= need:
            return ts[i - need + 1] - start_ts
    return None


class SimWorld:
    def __init__(self, engine: Engine, runtime=None):
        self.engine = engine
        self.telemetry = SimTelemetry(engine)
        self.runtime = runtime
        self._pcache: Optional[tuple] = None

    # ---- protocol
    def fork(self) -> "SimWorld":
        return SimWorld(self.engine.fork())

    def run_fast(self, seconds: float) -> None:
        ms = int(seconds * 1000)
        if self.runtime is not None:
            self.runtime.advance(ms)
        else:
            self.engine.step(int(round(ms / TICK)))

    def apply_action(self, kind: str, params: dict) -> dict:
        return self.engine.apply_action(kind, params or {})

    def success_rate_now(self, window_s: int = 30) -> float:
        e = self.engine
        with e.lock:
            a = e.sum_range(e.tick - int(window_s * 1000 / TICK), e.tick).sum(0)
        return round(100.0 * a[1] / max(int(a[0]), 1), 3)

    def ground_truth(self):
        return self.engine.ground_truth()

    def telemetry_hash(self) -> str:
        return self.engine.telemetry_hash()

    def measure_recovery(self, since_ts: int, tol: float = TOL_PP) -> Optional[int]:
        """Actual recovery time (ms) after `since_ts`, same definition as preview. None if not yet."""
        e = self.engine
        with e.lock:
            k_from = e.ts_to_tick(since_ts)
            n = (e.tick - k_from) // 4
            if n < 3:
                return None
            ts, s, _ = _trailing(e, k_from, n)
        return first_recovery(ts, s, e.ref_success - tol, since_ts)

    # ---- preview
    def _run_series(self, kind, params, horizon_s):
        f = self.engine.fork()
        if kind:
            f.apply_action(kind, params or {})
        k_from = f.tick
        n = int(horizon_s)
        f.step(n * 4)
        return _trailing(f, k_from, n)

    def preview_action(self, kind: Optional[str], params: dict, horizon_s: int = 120) -> dict:
        t_wall = time.perf_counter()
        e = self.engine
        key = (e.tick, e.cfg_version, horizon_s)
        if self._pcache and self._pcache[0] == key:
            base = self._pcache[1]
        else:
            base = self._run_series(None, {}, horizon_s)
            self._pcache = (key, base)
        pred = self._run_series(kind, params, horizon_s) if kind else base
        now = e.now
        ref_s, ref_t = e.ref_success, e.ref_tsr
        tech_degraded = min(base[2]) < ref_t - TOL_PP or min(pred[2]) < ref_t - TOL_PP
        if not tech_degraded:
            recovers, rec_ms = True, 0
        else:
            rec = first_recovery(pred[0], pred[1], ref_s - TOL_PP, now)
            recovers, rec_ms = (rec is not None), (rec if rec is not None else -1)
        r = lambda ts, v: [[int(t), round(float(x), 3)] for t, x in zip(ts, v)]
        return {"recovers": bool(recovers), "expected_recovery_ms": int(rec_ms),
                "predicted": r(pred[0], pred[1]), "baseline": r(base[0], base[1]),
                "technical_healthy": not tech_degraded,
                "improvement_pp": round(float(np.mean(pred[1][-10:]) - np.mean(base[1][-10:])), 3),
                "metric": "success_rate", "reference": round(ref_s, 3),
                "wall_ms": round((time.perf_counter() - t_wall) * 1000, 1)}
