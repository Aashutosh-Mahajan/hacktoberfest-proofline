"""Regressions for shared outages, unsupported claims and readable retry guidance."""
import copy
import json
import time

import pytest

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


def test_recovery_waits_for_healthy_payments_when_world_reports_percent_values():
    import threading

    world = FakeWorld("S1")
    values = iter([50.0, 70.0, 99.0])
    measured = []

    def success_rate(window):
        value = next(values)
        measured.append(value)
        world.now_ms += 1000
        return value

    world.success_rate_now = success_rate
    agent = Orchestrator(world, ListBus(), Stub([BATCH]), poll_s=0)
    executed = agent._remediate(world.incident(), {"id": "capacity", "kind": "scale_gateway",
                                "params": {}, "label": "Add capacity"}, None, threading.Event())
    assert measured == [50, 70, 99]
    assert executed["recovered"] and executed["actual_recovery_ms"] == 3000


def test_gemma_evidence_display_wrappers_are_normalized_without_inventing_proof():
    world = FakeWorld("S1")
    bus, agent = run(world, Stub([BATCH, [("submit_conclusion", conclusion(
        "deploy:482", "rollback_deploy", ["ev:ev_deploy_482", "[ev:ev_bank_BANK_X]", "ev:ev_deploy_482"]))]]))
    approve(bus, agent)
    assert bus.of("rca.ready")[-1]["rca"]["cause"] == "deploy:482"
    assert bus.of("verify.result")[-1]["passed"]
    missing = agent._norm_conclusion(conclusion("bank:BANK_ALL", "notify_only", ["ev:ev_bank_BANK_ALL"]), world.incident())
    assert not agent.verifier.verify(missing, world.incident()).passed


@pytest.mark.parametrize("scenario,cause,evidence,expected", [
    ("S1", "deploy:482", "ev_deploy_482", "rollback_deploy"),
    ("S2", "bank:BANK_X", "ev_bank_BANK_X", "notify_only"),
    ("S5", "business:limit_exceeded", "ev_breakdown_triage", "notify_only"),
])
def test_verified_cause_still_gets_a_response_when_gemma_omits_the_action(scenario, cause, evidence, expected):
    candidate = {"cause": cause, "confidence": .9, "evidence_ids": [evidence], "ruled_out": [], "summary": "Checked payment failures."}
    bus, agent = run(FakeWorld(scenario), Stub([BATCH, [("submit_conclusion", candidate)]]))
    proposal = approve(bus, agent)
    assert proposal["action"]["kind"] == expected
    assert bus.of("verify.result")[-1]["passed"]


def rate_limited_world():
    world = surge_world()
    world.sc["bd"]["top_codes"] = [{"code": "TD_RATE_LIMITED", "pct": 98}]
    return world


def test_repeated_wrong_model_claims_offer_verified_capacity_fix_for_approval():
    bad = conclusion("bank:BANK_ALL", "failover_bank", ["ev_bank_BANK_ALL"])
    world = rate_limited_world()
    bus, agent = run(world, Stub([BATCH, [("submit_conclusion", bad)]]))
    proposal = approve(bus, agent)
    assert proposal["action"]["kind"] == "scale_gateway"
    assert bus.of("verify.result")[-1]["passed"]
    assert bus.of("twin.preview")[-1]["recovers"]
    assert world.applied == [("scale_gateway", {})]
    assert "code: verified capacity runbook" in bus.of("rca.ready")[-1]["rca"]["summary"]


@pytest.mark.parametrize("condition", ["no_recovery", "normal_traffic", "single_bank", "competing_deploy"])
def test_capacity_runbook_cannot_force_an_unsupported_fix(condition):
    world = rate_limited_world()
    if condition == "no_recovery":
        world.previews["scale_gateway"] = False
    elif condition == "normal_traffic":
        world.sc["traffic"] = 1.0
    elif condition == "single_bank":
        for bank in ("BANK_Y", "BANK_Z"):
            world.sc["banks"][bank]["technical_success_rate"] = .995
    else:
        from fakes import D482
        world.sc["deploys"].append(D482)
    bad = conclusion("bank:BANK_ALL", "failover_bank", ["ev_bank_BANK_ALL"])
    bus, agent = run(world, Stub([BATCH, [("submit_conclusion", bad)]]))
    assert agent.join(5)
    assert not bus.of("action.proposed") and not world.applied


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_real_surge_simulation_offers_capacity_fix_after_model_rejections(seed):
    from mayday.core.bus import Bus
    from mayday.sim.runtime import Simulation

    events = Bus()
    simulation = Simulation(events, seed, "S4", 15, 1.0, emit_metrics=False)
    simulation.advance(40_000)
    incident = next(e["data"]["incident"] for e in events.since(0) if e["type"] == "incident.opened")
    bus = ListBus()
    bad = conclusion("bank:BANK_Y", "notify_only", ["ev_bank_BANK_Y"])
    agent = Orchestrator(simulation.world, bus, Stub([BATCH, [("submit_conclusion", bad)]]), poll_s=.01)
    agent.on_incident(incident)
    try:
        end = time.monotonic() + 5
        while not bus.of("action.proposed") and time.monotonic() < end:
            time.sleep(.01)
        proposal = bus.of("action.proposed")[0]
        assert proposal["action"]["kind"] == "scale_gateway"
        assert bus.of("twin.preview")[-1]["recovers"]
        assert bus.of("verify.result")[-1]["passed"]
        assert not bus.of("action.executed")  # still requires human approval
    finally:
        agent.reset()
        assert agent.join(2)


@pytest.mark.live
def test_live_gemma_surge_offers_a_verified_fix():
    from mayday.agent.gateway import OllamaGateway
    from mayday.core.bus import Bus
    from mayday.sim.runtime import Simulation

    events = Bus()
    simulation = Simulation(events, 1, "S4", 15, 1.0, emit_metrics=False)
    simulation.advance(40_000)
    incident = next(e["data"]["incident"] for e in events.since(0) if e["type"] == "incident.opened")
    gateway = OllamaGateway()
    bus = ListBus()
    agent = Orchestrator(simulation.world, bus, gateway, model_timeout_s=60, total_budget_s=210, poll_s=.05)
    agent.on_incident(incident)
    try:
        end = time.monotonic() + 220
        while time.monotonic() < end and not bus.of("action.proposed") and agent._thread.is_alive():
            time.sleep(.1)
        assert bus.of("action.proposed"), {"state": agent.snapshot(), "errors": bus.of("error")}
        assert bus.of("action.proposed")[-1]["action"]["kind"] == "scale_gateway"
        assert bus.of("verify.result")[-1]["passed"]
        assert bus.of("twin.preview")[-1]["recovers"]
        assert not bus.of("action.executed")
        assert gateway.calls
        print("\nLIVE RESULT:", json.dumps({"model": gateway.name, "model_calls": len(gateway.calls),
              "action": "scale_gateway", "source": bus.of("rca.ready")[-1]["rca"]["summary"],
              "checks_passed": True, "preview_recovers": True, "awaiting_human_approval": True}))
    finally:
        agent.reset()
        assert agent.join(3)
