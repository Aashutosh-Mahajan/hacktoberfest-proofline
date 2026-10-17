"""Deterministic incident detector (PRD FR-9..11). Evaluated once per simulated second.

Open rule: 5s success >= 5pp below the 5-min baseline (and 30s confirm >= 2pp below) for >= 10s.
(The PRD's 30s window alone cannot meet the <=12s detection NFR, so a 5s window opens and the 30s window confirms.)
Close rule: 10s success within 1.5pp of the frozen baseline for 15s.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from mayday.core.protocols import BANKS
from mayday.sim.engine import Engine, TICK, T0, TECH, AVG_PAISE
from mayday.sim.telemetry import SimTelemetry

SVC_BY_CODE = {"TD_TIMEOUT": "payment-orchestrator", "TD_BANK_5XX": "bank-adapter", "TD_RATE_LIMITED": "gateway"}


def _rate(a):
    return 100.0 * a[1] / max(int(a[0]), 1)


class Detector:
    def __init__(self, engine: Engine, telemetry: SimTelemetry):
        self.e = engine
        self.tel = telemetry
        self.inc: Optional[dict] = None
        self.baseline: Optional[float] = None
        self.cond_since: Optional[int] = None
        self.rec_since: Optional[int] = None
        self.cooldown_until = 0
        self.n = 0
        self.last_update = 0
        self.last_baseline = None
        self.last_close = 0

    def reset(self):
        self.__init__(self.e, self.tel)

    # ------------------------------------------------------------
    def _win(self, k1, ticks):
        return self.e.sum_range(k1 - ticks, k1).sum(0)

    def _baseline(self, k):
        e = self.e
        k0 = max(e.lo, k - 1320)
        k1 = k - 120
        if k1 - k0 < 200:
            return None
        k0, cnt, _, _ = e.range_arrays(k0, k1)
        nb = len(cnt) // 20
        c = cnt[: nb * 20].sum(1).reshape(nb, 20, -1).sum(1)
        r = 100.0 * c[:, 1] / np.maximum(c[:, 0], 1)
        return float(np.median(r))

    def observe(self) -> list:
        """Return list of (type, data, ts) events for the current sim time."""
        e = self.e
        k = e.tick
        now = e.now
        ev = []
        s5 = _rate(self._win(k, 20))
        if self.inc is None:
            base = self._baseline(k)
            if base is None or now < self.cooldown_until:
                self.cond_since = None
                return ev
            if self.last_baseline is not None and now - self.last_close < 360_000:
                base = max(base, self.last_baseline)
            s30 = _rate(self._win(k, 120))
            if s5 <= base - 5.0 and s30 <= base - 2.0:
                if self.cond_since is None:
                    self.cond_since = now
                if now - self.cond_since >= 10_000:
                    ev += self._open(base, now, s5)
            else:
                self.cond_since = None
        else:
            s10 = _rate(self._win(k, 40))
            if s10 >= self.baseline - 1.5:
                if self.rec_since is None:
                    self.rec_since = now - 10_000
                if now - self.rec_since >= 25_000:
                    inc = self.inc
                    end = self.rec_since
                    inc["end_ts"] = int(end)
                    inc["current_success"] = round(s10, 2)
                    ev.append(("incident.closed", {"incident_id": inc["id"], "end_ts": int(end),
                                                   "mttr_ms": int(end - inc["start_ts"])}, now))
                    self.closed = inc
                    self.last_baseline = self.baseline
                    self.last_close = now
                    self.cooldown_until = now + 30_000
                    self.inc = None
                    self.cond_since = None
            else:
                self.rec_since = None
            if self.inc is not None and now - self.last_update >= 2000:
                self.last_update = now
                self.inc["current_success"] = round(s5, 2)
                self.inc["decline_breakdown"] = self._breakdown(self.inc["start_ts"], now)
                self.inc["blast"] = self._blast(self.inc["start_ts"], now)
                ev.append(("incident.updated", {"incident": dict(self.inc)}, now))
        return ev

    # ------------------------------------------------------------
    def _changepoint(self, k) -> int:
        """Least-squares single step change over the last 150s of 1s bins -> start ts."""
        e = self.e
        k0, cnt, _, _ = e.range_arrays(max(e.lo, k - 600), k)
        nb = len(cnt) // 4
        c = cnt[: nb * 4].sum(1).reshape(nb, 4, -1).sum(1)
        tot = np.maximum(c[:, 0], 1).astype(float)
        r = c[:, 1] / tot
        w = tot
        best, bj = None, nb // 2
        cw = np.cumsum(w)
        cwr = np.cumsum(w * r)
        cwr2 = np.cumsum(w * r * r)
        for j in range(5, nb - 3):
            def sse(a, b):
                ww = cw[b - 1] - (cw[a - 1] if a else 0)
                s1 = cwr[b - 1] - (cwr[a - 1] if a else 0)
                s2 = cwr2[b - 1] - (cwr2[a - 1] if a else 0)
                return s2 - s1 * s1 / ww
            v = sse(0, j) + sse(j, nb)
            if best is None or v < best:
                best, bj = v, j
        return int(T0 + (k0 + bj * 4) * TICK)

    def _breakdown(self, start, end):
        b = self.tel.decline_breakdown(start, end, by=None)
        return {"technical_pct": b["technical_pct"], "business_pct": b["business_pct"], "top_codes": b["top_codes"]}

    def _blast(self, start, end) -> dict:
        e = self.e
        k1 = e.tick
        k0 = e.ts_to_tick(start)
        a = e.sum_range(k0, k1)
        base_fail = 100.0 - e.ref_success
        tot = a.sum(0)
        fail = int(tot[0] - tot[1])
        excess = max(0, int(fail - base_fail / 100.0 * tot[0]))
        # scope
        rates = [100.0 - _rate(a[i]) for i in range(3)]
        top = int(np.argmax(rates))
        others = [rates[i] for i in range(3) if i != top]
        biz = int(tot[5:8].sum())
        recent = e.sum_range(k1 - 120, k1)
        rt = recent.sum(0)
        failing_pct = 100.0 - _rate(rt)
        codes = tot[2:8]
        dom = int(np.argmax(codes))
        if rates[top] - np.mean(others) > 5.0 and rates[top] > 2 * max(np.mean(others), 1.0):
            svc = SVC_BY_CODE.get(("TD_TIMEOUT", "TD_BANK_5XX", "TD_RATE_LIMITED")[dom] if dom < 3 else "", "upi-service")
            scope = f"{BANKS[top]} via {svc}"
            rr = recent[top]
            failing_pct = 100.0 - _rate(rr)
        elif dom >= 3 and codes[3:].sum() > 0.7 * max(codes.sum(), 1):
            scope = "all banks (business declines: %s)" % ("BD_WRONG_PIN", "BD_INSUFFICIENT_FUNDS", "BD_LIMIT_EXCEEDED")[dom - 3]
        else:
            svc = SVC_BY_CODE.get(("TD_TIMEOUT", "TD_BANK_5XX", "TD_RATE_LIMITED")[dom] if dom < 3 else "", "gateway")
            scope = f"all banks via {svc}"
        k_pz = e.range_arrays(k1 - 120, k1)[2]
        failed_paise = int(k_pz[:, :, 0].sum())
        excess_rate = max(0.0, failed_paise * 2 - base_fail / 100.0 * int(k_pz[:, :, 1].sum()) * 2)
        return {"scope": scope, "failing_pct": round(float(failing_pct), 2), "failed_txns": int(excess),
                "paise_per_min_at_risk": int(excess_rate)}

    def _open(self, base, now, s5) -> list:
        e = self.e
        start = min(self._changepoint(e.tick), now - 1000)
        self.n += 1
        self.baseline = base
        self.rec_since = None
        self.last_update = now
        self.inc = {"id": f"inc_{self.n:04d}", "start_ts": int(start), "detected_ts": int(now),
                    "baseline_success": round(base, 2), "current_success": round(s5, 2),
                    "decline_breakdown": self._breakdown(start, now), "blast": self._blast(start, now)}
        return [("incident.opened", {"incident": dict(self.inc)}, now)]
