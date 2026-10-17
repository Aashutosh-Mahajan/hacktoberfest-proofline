"""Orchestrator behaviour with Gemma-shaped (sloppy, parallel) model output, via a stub gateway; plus an opt-in live test."""
import time

import pytest

from fakes import FakeWorld, ListBus
from mayday.agent import Orchestrator
from mayday.agent.gateway import Gateway, ModelError, ModelReply, ModelTimeout, ToolCall
from test_agent_integration import check_schema


class Stub(Gateway):
    backend = "ollama"
    name = "stub-gemma"

    def __init__(self, turns):
        self.turns, self.i, self.seen = turns, 0, []

    def chat(self, messages, tools=None, **kw):
        self.seen.append(kw)
        t = self.turns[min(self.i, len(self.turns) - 1)]
        self.i += 1
        if isinstance(t, Exception):
            raise t
        return ModelReply("", [ToolCall(f"c{self.i}_{n}", n, a) for n, a in t], via=self.name)

    def status(self):
        return {"backend": "ollama", "name": self.name, "healthy": True, "warm": True}


BATCH = [("bank_stats", {}), ("list_deploys", {}), ("traffic", {})]


def run(world, gw, **kw):
    bus = ListBus()
    o = Orchestrator(world, bus, gw, poll_s=0, **kw)
    o.heartbeat_s = 0
    o.on_incident(world.incident())
    return bus, o


def approve(bus, o):
    end = time.time() + 5
    while time.time() < end and not bus.of("action.proposed"):
        time.sleep(0.01)
    p = bus.of("action.proposed")[0]
    assert o.approve(p["incident_id"], p["action"]["id"], p["approval_token"])["ok"]
    assert o.join(5)
    return p


def test_two_calls_sloppy_conclusion_is_normalised_and_verified():
    concl = {"cause": "Deploy #482", "confidence": "95%", "evidence_ids": "ev_deploy_482, ev_bank_BANK_X",
             "ruled_out": ["bank_outage_BANK_X no, p95 normal", {"h_traffic": "traffic 1.0x"}, {"target": "business", "reason": "4% only"}],
             "action": "rollback", "summary": "Deploy 482 timeouts."}
    gw = Stub([BATCH, [("submit_conclusion", concl)]])
    w = FakeWorld("S1")
    bus, o = run(w, gw)
    approve(bus, o)
    assert sum(1 for k in gw.seen if 'num_predict' in k) == 2      # one batch turn + one conclusion turn
    assert [c["tool"] for c in bus.of("agent.tool_call")] == ["bank_stats", "list_deploys", "traffic"]
    rca = bus.of("rca.ready")[0]["rca"]
    assert rca["cause"] == "deploy:482" and rca["confidence"] == 0.95 and rca["summary"].endswith("(via stub-gemma)")
    a = rca["proposed_action"]
    assert a["kind"] == "rollback_deploy" and a["params"]["number"] == "482" and a["params"]["service"] == "payment-orchestrator"
    assert any("Requested in parallel" in t["text"] and "via stub-gemma" in t["text"] for t in bus.of("agent.thought"))
    assert all(len(t["text"]) <= 200 for t in bus.of("agent.thought"))
    assert gw.seen[0]["num_predict"] <= 256 and not gw.seen[0]["think"]
    check_schema(bus)


def test_verifier_rejection_is_fed_back_and_gemma_corrects():
    bad = {"cause": "bank:BANK_X", "confidence": 0.9, "evidence_ids": ["ev_bank_BANK_X"], "ruled_out": [], "action": "failover_bank", "summary": "bank"}
    good = {"cause": "deploy:482", "confidence": 0.9, "evidence_ids": ["ev_deploy_482", "ev_bank_BANK_X"], "ruled_out": [], "action": "rollback_deploy", "summary": "deploy"}
    gw = Stub([BATCH, [("submit_conclusion", bad)], [("submit_conclusion", good)]])
    bus, o = run(FakeWorld("S1"), gw)
    approve(bus, o)
    vr = bus.of("verify.result")
    assert not vr[0]["passed"] and vr[-1]["passed"] and o.verifier.catches == 1
    assert bus.of("rca.ready")[0]["rca"]["cause"] == "deploy:482"


def test_conclusion_in_same_turn_as_tools_is_not_trusted():
    concl = {"cause": "deploy:482", "confidence": 0.9, "evidence_ids": ["ev_deploy_482"], "ruled_out": [], "action": "rollback_deploy", "summary": "x"}
    gw = Stub([BATCH + [("submit_conclusion", concl)], [("submit_conclusion", dict(concl, evidence_ids=["ev_deploy_482", "ev_bank_BANK_X"]))]])
    bus, o = run(FakeWorld("S1"), gw)
    approve(bus, o)
    assert sum(1 for k in gw.seen if "num_predict" in k) == 2 and len(bus.of("verify.result")) == 1


def test_model_timeout_ends_honestly_without_fake_output():
    bus, o = run(FakeWorld("S1"), Stub([ModelTimeout("model call exceeded 240s")]))
    assert o.join(5)
    assert o.snapshot()["agent_state"] == "error"
    assert bus.of("error")[0]["code"] == "model_error" and not bus.of("rca.ready") and not bus.of("action.proposed")


def test_model_error_retries_once():
    gw = Stub([ModelError("boom"), BATCH, [("submit_conclusion", {"cause": "inconclusive", "confidence": 0.1, "evidence_ids": [], "ruled_out": [], "action": "", "summary": "unsure"})]])
    bus, o = run(FakeWorld("S1"), gw)
    assert o.join(5)
    assert bus.of("rca.ready")[0]["rca"]["inconclusive"] and gw.i == 3


def test_set_model_and_status():
    class G(Stub):
        def set_model(self, name):
            self.name = name
    bus, o = run(FakeWorld("S1"), G([BATCH]))
    o.join(5)
    assert o.set_model("gemma4:e2b")["name"] == "gemma4:e2b"
    assert o.model_status()["backend"] == "ollama"


def test_qa_through_model_with_read_only_tools():
    gw = Stub([[("bank_stats", {})], [("preview_fix", {"kind": "x"})]])
    w = FakeWorld("S1")
    bus = ListBus()
    o = Orchestrator(w, bus, gw, poll_s=0)
    o._incident = w.incident()
    q = o.ask("BANK_Y ka kya scene hai?")
    end = time.time() + 5
    while time.time() < end and not bus.of("voice.answer"):
        time.sleep(0.01)
    assert bus.of("voice.answer")[0]["query_id"] == q
    assert bus.of("voice.answer")[0]["evidence_ids"]


@pytest.mark.live
@pytest.mark.parametrize("scn,cause", [("S1", "deploy:482")])
def test_live_gemma_verified_rca(scn, cause):
    """Real Gemma via Ollama on the real simulator. Run: cd backend && python -m pytest tests/agent -m live -s"""
    from mayday.agent import make_gateway
    from mayday.eval.run import CaptureBus, find_detector, find_world_factory, make_world
    gw = make_gateway()
    if not gw.status()["healthy"]:
        pytest.skip("Ollama / gemma4:e4b not available")
    world = make_world(find_world_factory(), 1, scn, 30.0, 1.0)
    bus = CaptureBus()
    o = Orchestrator(world, bus, gw, model_timeout_s=240, total_budget_s=600, poll_s=0.1)
    detect, inc = find_detector(world), None
    for _ in range(60):
        world.run_fast(5)
        inc = detect()
        if inc:
            break
    assert inc
    t0 = time.perf_counter()
    o.on_incident(inc)
    approved = False
    while not o.join(0.05):
        p = bus.of("action.proposed")
        if p and not approved:
            approved = True
            o.approve(p[-1]["data"]["incident_id"], p[-1]["data"]["action"]["id"], p[-1]["data"]["approval_token"])
        world.run_fast(1)
        assert time.perf_counter() - t0 < 600
    rca = bus.of("rca.ready")[-1]["data"]["rca"]
    print("live", scn, rca["cause"], f"{time.perf_counter() - t0:.1f}s", o.model_stats())
    assert rca["cause"] == cause and "gemma" in rca["summary"].lower()
    assert any("via gemma" in e["data"]["text"] for e in bus.of("agent.thought"))


BANK_CONCL = {"cause": "bank:BANK_X", "confidence": 0.9, "evidence_ids": ["ev_bank_BANK_X"], "action": "failover_bank", "summary": "BANK_X is down.",
              "ruled_out": [{"target": "h_traffic", "reason": "1.0x"}, {"target": "business_decline", "reason": "5%"}]}


def test_failover_is_explicit_with_warnings():
    gw = Stub([BATCH, [("submit_conclusion", BANK_CONCL)]])
    bus, o = run(FakeWorld("S2", previews={"failover_bank": {"recovers": True, "target_bank": "BANK_Y", "target_headroom_pct": 40,
                                                              "warnings": ["twin: BANK_Y p95 rises 20%"]}}), gw)
    approve(bus, o)
    a = bus.of("action.proposed")[0]["action"]
    assert a["kind"] == "notify_only" and a["params"] == {}
    assert "contacting" in a["rationale"]
    assert not bus.of("twin.preview") and not bus.of("impact.savings")


def test_target_overload_picks_alternative_not_silent_failover():
    gw = Stub([BATCH, [("submit_conclusion", BANK_CONCL)]])
    bus, o = run(FakeWorld("S2", previews={"failover_bank": {"recovers": True, "target_headroom_pct": 5}}), gw)
    approve(bus, o)
    vr = bus.of("verify.result")
    assert not bus.of("twin.preview")
    a = bus.of("action.proposed")[0]["action"]
    assert a["kind"] == "notify_only" and vr[-1]["passed"]
    assert bus.of("rca.ready")[0]["rca"]["cause"] == "bank:BANK_X"


def test_nothing_safe_proposes_honest_notify_only_with_warnings():
    gw = Stub([BATCH, [("submit_conclusion", BANK_CONCL)]])
    bad = {"recovers": True, "recovery_pct": 40, "warnings": ["only 40% of traffic recovers"]}
    bus, o = run(FakeWorld("S2", previews={"failover_bank": bad, "enable_circuit_breaker": False}), gw)
    approve(bus, o)
    a = bus.of("action.proposed")[0]["action"]
    assert a["kind"] == "notify_only" and "No safe automatic fix" in a["rationale"] and "contacting" in a["rationale"]
    assert not bus.of("twin.preview")
    assert "No safe automatic fix" in bus.of("rca.ready")[0]["rca"]["summary"]
    assert not any(e["code"] == "action_failed" for e in bus.of("error"))


def _wait(bus, t, n=5):
    end = time.time() + n
    while time.time() < end and not bus.of(t):
        time.sleep(0.01)
    return bus.of(t)


class TextStub(Stub):
    text = None

    def __init__(self, turns):
        super().__init__(turns)
        self.prompts = []

    def chat(self, messages, tools=None, **kw):
        if tools is None and "TASK:" in str(messages[-1].get("content")) and self.text is not None:
            self.prompts.append(messages)
            if isinstance(self.text, Exception):
                raise self.text
            return ModelReply(self.text, [], via=self.name)
        return super().chat(messages, tools, **kw)


def _fixed_incident():
    concl = {"cause": "deploy:482", "confidence": 0.9, "evidence_ids": ["ev_deploy_482", "ev_bank_BANK_X"], "ruled_out": [], "action": "rollback_deploy", "summary": "d"}
    gw = TextStub([BATCH, [("submit_conclusion", concl)]])
    bus, o = run(FakeWorld("S1"), gw)
    return bus, o, gw


def test_followup_refused_until_fix_applied():
    bus, o, gw = _fixed_incident()
    _wait(bus, "action.proposed")
    iid = bus.of("action.proposed")[0]["incident_id"]
    assert o.followup(iid, "permanent_fix") == {"ok": False, "error": "no applied fix yet"}
    assert o.followup(iid, "bogus")["ok"] is False and o.followup("other", "permanent_fix")["ok"] is False
    approve(bus, o)
    assert o.followup(iid, "permanent_fix") == {"ok": True}


@pytest.mark.parametrize("kind,aud", [("permanent_fix", "Engineering"), ("escalate_bank", "BANK_X"), ("merchant_notice", "Merchants"), ("followup_tasks", "Team")])
def test_followup_written_by_gemma_with_real_facts(kind, aud):
    bus, o, gw = _fixed_incident()
    gw.text = "Draft body " + "word " * 300
    approve(bus, o)
    assert o.followup(bus.of("action.proposed")[0]["incident_id"], kind)["ok"]
    d = _wait(bus, "followup.ready")[0]
    assert d["kind"] == kind and d["audience"] == aud and d["simulated"] is True and "(template)" not in d["title"]
    assert len(d["body"].split()) <= 222
    prompt = str(gw.prompts[-1])
    assert "deploy:482" in prompt and "ev_deploy_482" in prompt and "payment-orchestrator" in prompt


def test_followup_template_fallback_is_marked():
    bus, o, gw = _fixed_incident()
    gw.text = ModelError("down")
    approve(bus, o)
    o.followup(bus.of("action.proposed")[0]["incident_id"], "merchant_notice")
    d = _wait(bus, "followup.ready")[0]
    assert d["title"].endswith("(template)") and "भुगतान" in d["body"]
