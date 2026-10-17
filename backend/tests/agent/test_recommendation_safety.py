"""Regressions for shared outages, unsupported claims and readable retry guidance."""
import copy
import json

from fakes import FakeWorld, ListBus
from test_gemma_flow import BATCH, Stub, approve, run
from mayday.agent import Orchestrator
from mayday.agent.compact import compact_messages, render_context


def surge_world():
    world = FakeWorld("S2")
    world.sc = copy.deepcopy(world.sc)
    world.sc["traffic"] = 2.05
    world.sc["bulletin"] = None
    for bank in world.sc["banks"].values():
        bank["technical_success_rate"] = bank["success_rate"] = 0.50
    return world


def conclusion(cause, action, evidence):
    return {"cause": cause, "confidence": .95, "action": action, "evidence_ids": evidence,
            "ruled_out": [], "summary": "Payment requests have increased and banks are struggling."}


def test_shared_outage_rejects_single_bank_then_accepts_evidenced_capacity_fix():
    bad = conclusion("bank:BANK_Y", "failover_bank", ["ev_bank_BANK_Y"])
    good = conclusion("traffic_spike", "scale_gateway", ["ev_metric_traffic", "ev_bank_BANK_Y"])
    bus, agent = run(surge_world(), Stub([BATCH, [("submit_conclusion", bad)], [("submit_conclusion", good)]]))
    proposal = approve(bus, agent)
    assert proposal["action"]["kind"] == "scale_gateway"
    assert not bus.of("verify.result")[0]["passed"]
    assert bus.of("verify.result")[-1]["passed"]
    assert "worst_bank" not in agent._ctx
    assert "Several negative values" in render_context(agent._ctx)


def test_invented_bank_never_becomes_an_approvable_fix():
    bad = conclusion("bank:BANK_ALL", "failover_bank", ["ev_bank_BANK_ALL"])
    bus, agent = run(surge_world(), Stub([BATCH, [("submit_conclusion", bad)]]))
    assert agent.join(5)
    assert not bus.of("action.proposed")
    assert bus.of("rca.ready")[-1]["rca"]["inconclusive"]


def test_traffic_claim_requires_traffic_evidence_and_unknown_cause_is_rejected():
    world = surge_world()
    agent = Orchestrator(world, ListBus(), Stub([BATCH]))
    agent._triage(world.incident())
    agent.tools.call("bank_stats", {})
    for cause, action, rule in [("traffic_spike", "scale_gateway", "evidence_exists"),
                                ("shared_magic", "scale_gateway", "cause_supported")]:
        result = agent.verifier.verify(conclusion(cause, action, ["ev_bank_BANK_X"]), world.incident())
        assert not result.passed
        assert rule in [check.name for check in result.failed]


def test_unknown_receiving_bank_is_rejected_even_if_preview_claims_recovery():
    world = FakeWorld("S2")
    agent = Orchestrator(world, ListBus(), Stub([BATCH]))
    check = agent.verifier.failover_check(
        {"params": {"bank": "BANK_X", "to_bank": "BANK_ALL"}},
        {"recovers": True, "target_headroom_pct": 100}, world.incident())
    assert not check.passed and check.code == "UNKNOWN_BANK"


def test_compact_retry_keeps_instructions_to_recheck_shared_causes():
    feedback = {"checks": [{"rule": "others_healthy", "passed": False, "detail": "all banks failing"}],
                "instruction": "Recheck traffic with tools; never invent BANK_ALL."}
    messages = compact_messages([{"role": "user", "content": "VERIFIER_REJECTED\n" + json.dumps(feedback)}])
    assert feedback["instruction"] in messages[0]["content"]
    assert "Do not gather more data" not in messages[0]["content"]
