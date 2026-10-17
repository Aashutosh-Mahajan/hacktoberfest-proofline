"""Self-contained FakeWorld/FakeTelemetry implementing core.protocols with hand-built S1/S2/S3/S5 data."""
from __future__ import annotations

import copy

T0 = 1_700_000_000_000  # dip start
BANKS = ("BANK_X", "BANK_Y", "BANK_Z")
BASE = {"success_rate": 0.995, "technical_success_rate": 0.995, "p95_ms": 400, "tps": 100, "error_5xx_pct": 0.1}


def _bank(tsr, p95=400, e5=0.1, sr=None):
    return {"success_rate": sr if sr is not None else tsr, "technical_success_rate": tsr, "p95_ms": p95, "tps": 100, "error_5xx_pct": e5}


OLD_DEPLOY = {"id": "dep_481", "service": "wallet", "number": 481, "ts": T0 - 3 * 3600_000, "author": "a", "summary": "wallet tweak", "diff": "- a\n+ b"}
D482 = {"id": "dep_482", "service": "payment-orchestrator", "number": 482, "ts": T0 - 3000, "author": "dev1",
        "summary": "Tune BANK_X timeout 3000->800ms", "diff": "- bank_timeout_ms.BANK_X: 3000\n+ bank_timeout_ms.BANK_X: 800"}
D483 = {"id": "dep_483", "service": "notification-service", "number": 483, "ts": T0 + 41_000, "author": "dev2",
        "summary": "Update push templates", "diff": "- tmpl v1\n+ tmpl v2"}

SCEN = {
    "S1": dict(deploys=[OLD_DEPLOY, D482], banks={"BANK_X": _bank(0.55, 810, 0.1, 0.55), "BANK_Y": _bank(0.995), "BANK_Z": _bank(0.995)},
               bd={"technical_pct": 96.0, "business_pct": 4.0, "top_codes": [{"code": "TD_TIMEOUT", "pct": 90.0}, {"code": "BD_WRONG_PIN", "pct": 4.0}]},
               traffic=1.02, bulletin=None, truth=None, cur=0.83),
    "S2": dict(deploys=[OLD_DEPLOY], banks={"BANK_X": _bank(0.62, 2400, 18.0), "BANK_Y": _bank(0.995), "BANK_Z": _bank(0.995)},
               bd={"technical_pct": 95.0, "business_pct": 5.0, "top_codes": [{"code": "TD_BANK_5XX", "pct": 70.0}, {"code": "TD_TIMEOUT", "pct": 25.0}]},
               traffic=1.0, bulletin="ok", truth=(T0, T0 + 60_000), cur=0.87),
    "S3": dict(deploys=[OLD_DEPLOY, D483], banks={"BANK_X": _bank(0.62, 2400, 18.0), "BANK_Y": _bank(0.995), "BANK_Z": _bank(0.995)},
               bd={"technical_pct": 95.0, "business_pct": 5.0, "top_codes": [{"code": "TD_BANK_5XX", "pct": 70.0}, {"code": "TD_TIMEOUT", "pct": 25.0}]},
               traffic=1.0, bulletin="ok", truth=(T0, T0 + 60_000), cur=0.87),
    "S5": dict(deploys=[OLD_DEPLOY], banks={b: _bank(0.99, 420, 0.1, 0.78) for b in BANKS},
               bd={"technical_pct": 8.0, "business_pct": 92.0, "top_codes": [{"code": "BD_LIMIT_EXCEEDED", "pct": 85.0}, {"code": "BD_WRONG_PIN", "pct": 7.0}]},
               traffic=1.2, bulletin=None, truth=None, cur=0.78),
}


class FakeTelemetry:
    def __init__(self, world):
        self.w = world

    @property
    def s(self):
        return self.w.sc

    def now(self):
        return self.w.now_ms

    def _incident_phase(self, end_ts):
        return end_ts > T0

    def _banks(self, end_ts):
        if not self._incident_phase(end_ts) or self.w.recovered:
            return {b: dict(BASE) for b in BANKS}
        return copy.deepcopy(self.s["banks"])

    def success_series(self, start_ts, end_ts, by=None, step_ms=5000, technical_only=False):
        banks = self._banks(end_ts)
        pts = lambda v0, v1: [[start_ts, v0], [end_ts, v1]]
        if by == "bank":
            return {"series": {b: pts(0.995, v["technical_success_rate"]) for b, v in banks.items()}}
        return {"series": {"all": pts(0.995, self.s["cur"])}}

    def decline_breakdown(self, start_ts, end_ts, by=None):
        r = copy.deepcopy(self.s["bd"])
        if by == "bank":
            r["by"] = {b: {"technical_pct": r["technical_pct"], "business_pct": r["business_pct"]} for b in BANKS}
        return r

    def bank_stats(self, start_ts, end_ts):
        return self._banks(end_ts)

    def query_logs(self, start_ts, end_ts, service=None, bank=None, code=None, limit=20):
        lines = [{"ts": T0 + 1000 * i, "service": "bank-adapter", "bank": bank or "BANK_X", "level": "ERROR",
                  "code": code or "TD_TIMEOUT", "msg": f"{code or 'TD_TIMEOUT'} calling {bank or 'BANK_X'}"} for i in range(3)]
        return {"lines": lines[:limit], "total": 37}

    def deploys(self, start_ts, end_ts):
        return [d for d in self.s["deploys"] if start_ts <= d["ts"] <= end_ts]

    def get_deploy(self, deploy_id):
        return next((d for d in self.s["deploys"] if d["id"] == deploy_id), None)

    def traffic(self, start_ts, end_ts):
        return {"tps_series": [[start_ts, 300]], "baseline_tps": 300.0, "ratio": self.s["traffic"]}

    def read_bulletin(self, bank):
        if self.s["bulletin"] and bank == "BANK_X":
            end = T0 + 600_000 if self.w.bulletin_ok else T0 + 10_000
            return {"png_path": "x.png", "image_url": "/api/bulletin/BANK_X.png",
                    "extracted": {"bank": "BANK_X", "status": "degraded", "claimed_start_ts": T0 - 5000, "claimed_end_ts": end}}
        return None

    def bulletin_truth_window(self, bank):
        return self.s["truth"] if bank == "BANK_X" else None


class FakeWorld:
    def __init__(self, scenario="S1", bulletin_ok=True, previews=None):
        self.name = scenario
        self.sc = SCEN[scenario]
        self.now_ms = T0 + 60_000
        self.bulletin_ok = bulletin_ok
        self.recovered = False
        self.applied = []
        self.previews = previews or {}
        self.telemetry = FakeTelemetry(self)

    def incident(self):
        return {"id": f"inc_{self.name}", "start_ts": T0, "detected_ts": T0 + 12_000, "baseline_success": 0.99,
                "current_success": self.sc["cur"]}

    def fork(self):
        return copy.copy(self)

    def run_fast(self, seconds):
        self.now_ms += int(seconds * 1000)

    def apply_action(self, kind, params):
        self.applied.append((kind, params))
        if kind != "notify_only":
            self.recovered = True
            self._act_ts = self.now_ms
        return {"ok": True, "detail": "applied"}

    def preview_action(self, kind, params, horizon_s=120):
        if kind in self.previews:
            rec = self.previews[kind]
        else:
            rec = kind != "notify_only"
        return {"recovers": rec, "expected_recovery_ms": 8000 if rec else 0,
                "predicted": [[self.now_ms, 0.8], [self.now_ms + 8000, 0.99]], "baseline": [[self.now_ms, 0.8], [self.now_ms + 8000, 0.8]]}

    def success_rate_now(self, window_s=30):
        if self.recovered:
            self.now_ms += 3000
            return 0.99
        return self.sc["cur"]

    def ground_truth(self):
        return None


class ListBus:
    """Minimal EventBus for tests."""

    def __init__(self):
        self.events = []

    def emit(self, type, data, ts=None):
        self.events.append({"type": type, "data": data, "ts": ts})
        return len(self.events)

    def of(self, t):
        return [e["data"] for e in self.events if e["type"] == t]

    def types(self):
        return [e["type"] for e in self.events]
