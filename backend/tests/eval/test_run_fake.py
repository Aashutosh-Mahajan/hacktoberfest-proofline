import asyncio
import random

from mayday.eval import run as R


class FakeWorld:
    def __init__(self, seed, scenario):
        self.scenario, self.t = scenario, 0
        self.telemetry = None

    def run_fast(self, s):
        self.t += int(s * 1000)

    def ground_truth(self):
        return None if self.scenario == "S0" else {"scenario": self.scenario, "cause": "deploy:482"}


class FakeOrch:
    def __init__(self, world, bus, gw, verifier_enabled=True):
        self.bus, self.ver = bus, verifier_enabled

    def on_incident(self, inc):
        b = self.bus
        b.emit("agent.tool_call", {"tool": "x"})
        b.emit("verify.result", {"passed": False})
        b.emit("verify.result", {"passed": True})
        b.emit("rca.ready", {"rca": {"cause": "deploy:482", "proposed_action": {"id": "a"}}}, ts=40_000)
        b.emit("action.proposed", {"incident_id": "i", "action": {"id": "a", "kind": "rollback_deploy"}, "approval_token": "t"})

    def join(self, t=None):
        return self.done

    done = False

    def approve(self, *a):
        self.done = True
        self.bus.emit("action.executed", {"predicted_recovery_ms": 20000, "actual_recovery_ms": 25000})


def test_run_one_with_fakes(monkeypatch):
    monkeypatch.setattr(R, "make_orchestrator", lambda w, b, v, wf=True: FakeOrch(w, b, None, v))
    monkeypatch.setattr(R, "find_detector",
                        lambda w: (lambda: None if w.scenario == "S0" or w.t < 30000 else {"detected_ts": 30_000}))
    f = lambda seed, scenario, fault_offset_s, severity: FakeWorld(seed, scenario)
    r = asyncio.run(R.run_one(f, "S1", 1, True, random.Random(1)))
    assert r["correct"] and r["remediated"] and r["tool_calls"] == 1
    assert r["verifier_rejections"] == 1 and r["conclusion_attempts"] == 2
    assert r["twin_error_s"] == -5.0 and r["rca_s"] == 10.0
    r0 = asyncio.run(R.run_one(f, "S0", 1, True, random.Random(1)))
    assert not r0["incident"] and r0["correct"]
