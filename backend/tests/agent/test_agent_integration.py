import time

import pytest

from fakes import FakeWorld, ListBus, T0
from mayday.agent import Orchestrator, ScriptedGateway
from mayday.agent.evidence import EvidenceStore
from mayday.agent.verifier import Verifier

# ---- hand-written schema checks derived from contracts/types.ts EventMap (required keys per event)
SCHEMA = {
    "agent.state": {"incident_id", "state"},
    "agent.thought": {"incident_id", "text"},
    "agent.tool_call": {"incident_id", "call_id", "tool", "args"},
    "agent.tool_result": {"incident_id", "call_id", "tool", "summary", "ok", "evidence_ids", "ms"},
    "hypothesis.update": {"incident_id", "hypothesis"},
    "verify.result": {"incident_id", "attempt", "passed", "checks"},
    "rca.ready": {"rca"},
    "twin.preview": {"incident_id", "action_id", "kind", "recovers", "expected_recovery_ms", "predicted", "baseline"},
    "action.proposed": {"incident_id", "action", "approval_token", "expires_ts"},
    "action.executed": {"incident_id", "action_id", "kind", "recovered"},
    "impact.savings": {"incident_id", "actual_loss_paise", "counterfactual_loss_paise", "saved_paise", "assumed_manual_mttr_ms"},
    "comms.drafted": {"incident_id", "drafts"},
    "postmortem.ready": {"incident_id", "markdown"},
    "voice.query": {"query_id", "transcript", "lang"},
    "voice.answer": {"query_id", "text", "lang", "evidence_ids"},
    "error": {"code", "message", "recoverable"},
    "incident.updated": {"incident"},
}
STATES = {"idle", "triage", "investigating", "concluding", "verifying", "previewing", "proposing", "awaiting_approval",
          "remediating", "comms", "postmortem", "inconclusive", "error"}
HYP_KINDS = {"deploy", "bank_outage", "traffic_spike", "config_drift", "business_decline"}
HYP_STATUS = {"open", "supported", "ruled_out", "confirmed"}


def check_schema(bus):
    for e in bus.events:
        need = SCHEMA.get(e["type"])
        if need is None:
            continue
        assert need <= set(e["data"]), (e["type"], need - set(e["data"]))
        if e["type"] == "agent.state":
            assert e["data"]["state"] in STATES
        if e["type"] == "hypothesis.update":
            h = e["data"]["hypothesis"]
            assert {"id", "kind", "target", "status", "reason"} <= set(h)
            assert h["kind"] in HYP_KINDS and h["status"] in HYP_STATUS and len(h["reason"]) <= 120
        if e["type"] == "verify.result":
            assert all({"name", "passed", "detail"} == set(c) for c in e["data"]["checks"])
        if e["type"] == "rca.ready":
            r = e["data"]["rca"]
            assert {"incident_id", "cause", "cause_label", "confidence", "evidence", "ruled_out", "summary"} <= set(r)


def run(scn, wrong_first=False, approve=True, **kw):
    world, bus = FakeWorld(scn, **kw.pop("world_kw", {})), ListBus()
    orch = Orchestrator(world, bus, ScriptedGateway(wrong_first=wrong_first), poll_s=0, **kw)
    orch.on_incident(world.incident())
    return world, bus, orch


def wait_state(orch, state, t=5):
    end = time.time() + t
    while time.time() < end:
        if orch.snapshot()["agent_state"] == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"never reached {state}: {orch.snapshot()['agent_state']}")


def approve(bus, orch):
    wait_state(orch, "awaiting_approval")
    p = bus.of("action.proposed")[0]
    r = orch.approve(p["incident_id"], p["action"]["id"], p["approval_token"])
    assert r == {"ok": True}
    assert orch.join(5)
    return p


@pytest.mark.parametrize("scn,cause,kind", [("S1", "deploy:482", "rollback_deploy"), ("S2", "bank:BANK_X", "failover_bank"),
                                              ("S3", "bank:BANK_X", "failover_bank"), ("S5", "business:limit_exceeded", "notify_only")])
def test_scenarios_reach_verified_rca(scn, cause, kind):
    world, bus, orch = run(scn)
    p = approve(bus, orch)
    rca = bus.of("rca.ready")[0]["rca"]
    assert rca["cause"] == cause and not rca.get("inconclusive")
    assert rca["proposed_action"]["kind"] == kind
    assert bus.of("verify.result")[-1]["passed"]
    assert bus.of("action.executed")[0]["kind"] == kind
    assert bus.of("comms.drafted")[0]["drafts"].__len__() == 6
    assert "Timeline" in bus.of("postmortem.ready")[0]["markdown"]
    # evidence resolves
    for ev in rca["evidence"]:
        assert orch.get_evidence(ev["id"])["id"] == ev["id"]
    if kind == "notify_only":
        assert world.applied[0][0] == "notify_only" and not bus.of("twin.preview")
        assert not bus.of("impact.savings")
    else:
        ex = bus.of("action.executed")[0]
        assert ex["recovered"] and ex["predicted_recovery_ms"] == 8000 and ex["actual_recovery_ms"] > 0
        sv = bus.of("impact.savings")[0]
        assert sv["saved_paise"] > 0 or sv["counterfactual_loss_paise"] == 0
        assert bus.of("twin.preview")
    assert orch.last_result()["agent_cause"] == cause and orch.last_result()["action_proposed"]
    check_schema(bus)
    assert orch.snapshot()["agent_state"] == "idle"


def test_s3_decoy_cleared_with_timing_reason():
    world, bus, orch = run("S3")
    approve(bus, orch)
    rca = bus.of("rca.ready")[0]["rca"]
    r = next(x for x in rca["ruled_out"] if x["target"] == "deploy:483")
    assert "AFTER" in r["reason"] and len(r["reason"]) <= 120


def test_s3_wrong_first_is_rejected_then_corrected():
    world, bus, orch = run("S3", wrong_first=True)
    approve(bus, orch)
    vr = bus.of("verify.result")
    assert not vr[0]["passed"] and vr[-1]["passed"] and len(vr) == 2
    failed = {c["name"] for c in vr[0]["checks"] if not c["passed"]}
    assert {"deploy_precedes_dip", "deploy_on_failing_path"} <= failed
    assert "483" in vr[0]["rejected_reason"]
    assert orch.verifier.catches == 1
    assert bus.of("rca.ready")[0]["rca"]["cause"] == "bank:BANK_X"
    check_schema(bus)


def test_s2_contradicted_bulletin_caught_then_dropped():
    world, bus, orch = run("S2", wrong_first=True, world_kw={"bulletin_ok": False})
    approve(bus, orch)
    vr = bus.of("verify.result")
    assert not vr[0]["passed"]
    assert any(c["name"] == "external_claim_consistent" and not c["passed"] for c in vr[0]["checks"])
    rca = bus.of("rca.ready")[0]["rca"]
    assert rca["cause"] == "bank:BANK_X" and not any(e["kind"] == "bulletin" for e in rca["evidence"])


def test_twin_rejection_picks_alternative_or_none():
    world, bus, orch = run("S1", world_kw={"previews": {"rollback_deploy": False}})
    approve(bus, orch)
    vr = bus.of("verify.result")
    assert not vr[0]["passed"] and any(c["name"] == "twin_predicts_recovery" and not c["passed"] for c in vr[0]["checks"])
    assert bus.of("action.proposed")[0]["action"]["kind"] == "failover_bank"


def test_token_single_use_and_binding():
    world, bus, orch = run("S1")
    wait_state(orch, "awaiting_approval")
    p = bus.of("action.proposed")[0]
    iid, aid, tok = p["incident_id"], p["action"]["id"], p["approval_token"]
    assert orch.approve(iid, aid, "nope")["error"] == "bad_token"
    assert orch.approve(iid, "other", tok)["error"] == "action_mismatch"
    assert orch.approve("other", aid, tok)["error"] == "no_pending_action"
    assert orch.approve(iid, aid, tok)["ok"]
    assert orch.approve(iid, aid, tok) == {"ok": False, "error": "token_already_used"}
    assert orch.join(5)


def test_token_expiry():
    t = [0.0]
    world, bus, orch = run("S1", clock=lambda: t[0], approval_ttl_s=300, total_budget_s=10**9)
    wait_state(orch, "awaiting_approval")
    p = bus.of("action.proposed")[0]
    t[0] = 301.0
    assert orch.approve(p["incident_id"], p["action"]["id"], p["approval_token"])["error"] == "token_expired"
    assert orch.join(5)
    assert any(e["code"] == "approval_expired" for e in bus.of("error"))
    assert not bus.of("action.executed")


def test_reject_skips_remediation():
    world, bus, orch = run("S1")
    wait_state(orch, "awaiting_approval")
    p = bus.of("action.proposed")[0]
    assert orch.reject(p["incident_id"], p["action"]["id"])["ok"]
    assert orch.join(5)
    assert not world.applied and bus.of("postmortem.ready")


class LoopGateway(ScriptedGateway):
    """Never concludes: keeps calling tools."""
    def chat(self, messages, tools=None, **kw):
        from mayday.agent.gateway import ModelReply, ToolCall
        return ModelReply("still looking", [ToolCall("c", "traffic", {})])


class SlowGateway(ScriptedGateway):
    pass


def test_budget_exhaustion_is_inconclusive():
    world, bus = FakeWorld("S1"), ListBus()
    orch = Orchestrator(world, bus, LoopGateway(), poll_s=0)
    orch.on_incident(world.incident())
    assert orch.join(5)
    rca = bus.of("rca.ready")[0]["rca"]
    assert rca["inconclusive"] and rca["cause"] == "inconclusive"
    assert len(bus.of("agent.tool_call")) == 10
    assert orch.snapshot()["agent_state"] == "inconclusive"
    assert not bus.of("action.proposed")


def test_time_budget_is_inconclusive():
    world, bus = FakeWorld("S1"), ListBus()
    orch = Orchestrator(world, bus, ScriptedGateway(latency_s=0.05), total_budget_s=0.12, poll_s=0)
    orch.on_incident(world.incident())
    assert orch.join(5)
    assert bus.of("rca.ready")[0]["rca"].get("inconclusive")


def test_model_crash_is_recoverable_error():
    from mayday.agent.gateway import ModelError

    class Boom(ScriptedGateway):
        def chat(self, *a, **k):
            raise ModelError("down")
    world, bus = FakeWorld("S1"), ListBus()
    orch = Orchestrator(world, bus, Boom(), poll_s=0)
    orch.on_incident(world.incident())
    assert orch.join(5)
    assert bus.of("error")[0]["recoverable"] and orch.snapshot()["agent_state"] == "error"


def test_ask_hinglish_and_english():
    world, bus = FakeWorld("S1"), ListBus()
    orch = Orchestrator(world, bus, ScriptedGateway(), poll_s=0)
    orch._incident = world.incident()
    q = orch.ask("BANK_Y ka kya scene hai?")
    end = time.time() + 5
    while time.time() < end and not bus.of("voice.answer"):
        time.sleep(0.01)
    a = bus.of("voice.answer")[0]
    assert a["query_id"] == q and a["lang"] == "hinglish" and "BANK_Y" in a["text"] and "nahi" in a["text"]
    assert bus.of("voice.query")[0]["transcript"].startswith("BANK_Y")
    check_schema(bus)
    assert orch.ask_audio(b"x", "audio/webm")
    assert bus.of("error")[-1]["code"] == "audio_unsupported"


# ---- verifier unit tests (key rules, pass and fail)
def _v(scn):
    w = FakeWorld(scn)
    st = EvidenceStore()
    for i in ("ev_deploy_482", "ev_deploy_483", "ev_bank_BANK_X", "ev_bank_BANK_Y", "ev_breakdown_bank", "ev_metric_traffic"):
        st.register(i, "metric", i, {})
    return Verifier(w.telemetry, st), w.incident()


def _c(cause, ev, action=None, ruled=()):
    return {"cause": cause, "evidence_ids": ev, "ruled_out": [{"target": t, "reason": "x"} for t in ruled], "proposed_action": action}


def names(res, passed):
    return {c.name for c in res.checks if c.passed == passed}


def test_verifier_s1_pass_and_wrong_bank_fail():
    v, inc = _v("S1")
    act = {"kind": "rollback_deploy", "params": {"number": "482"}}
    ok = v.verify(_c("deploy:482", ["ev_deploy_482", "ev_bank_BANK_X"], act), inc)
    assert ok.passed and {"deploy_precedes_dip", "scope_concentration", "others_healthy", "decline_class_matches", "action_matches_cause"} <= names(ok, True)
    bad = v.verify(_c("bank:BANK_X", ["ev_bank_BANK_X"], {"kind": "failover_bank", "params": {"bank": "BANK_X"}}, ["deploy:482"]), inc)
    assert "no_internal_cause" in names(bad, False)
    badact = v.verify(_c("deploy:482", ["ev_deploy_482"], {"kind": "notify_only", "params": {}}), inc)
    assert "action_matches_cause" in names(badact, False)
    missing = v.verify(_c("deploy:482", ["ev_nope"]), inc)
    assert "evidence_exists" in names(missing, False)
    assert v.catches == 3


def test_verifier_s3_decoy_and_s5_business():
    v, inc = _v("S3")
    bad = v.verify(_c("deploy:483", ["ev_deploy_483"], {"kind": "rollback_deploy", "params": {"number": "483"}}), inc)
    assert {"deploy_precedes_dip", "deploy_on_failing_path"} <= names(bad, False)
    ok = v.verify(_c("bank:BANK_X", ["ev_bank_BANK_X"], {"kind": "failover_bank", "params": {"bank": "BANK_X"}}, ["deploy:483"]), inc)
    assert ok.passed
    unruled = v.verify(_c("bank:BANK_X", ["ev_bank_BANK_X"]), inc)
    assert "no_internal_cause" in names(unruled, False)
    v5, inc5 = _v("S5")
    assert v5.verify(_c("business:limit_exceeded", ["ev_breakdown_bank"], {"kind": "notify_only", "params": {}}), inc5).passed
    wrong = v5.verify(_c("bank:BANK_X", ["ev_bank_BANK_X"], {"kind": "failover_bank", "params": {"bank": "BANK_X"}}), inc5)
    assert {"decline_class_matches", "scope_concentration"} & names(wrong, False)
    t = v5.verify(_c("traffic_spike", ["ev_metric_traffic"]), inc5)
    assert "traffic_elevated" in names(t, False)


# ---- gateway utilities
def test_json_repair():
    from mayday.agent.util import parse_json_loose
    assert parse_json_loose("```json\n{'a': 1,}\n```") == {"a": 1}
    assert parse_json_loose('sure! {"name": "x", "arguments": {"b": 2}} done')["arguments"] == {"b": 2}


def test_ollama_gateway_stream_and_reask():
    import json
    import httpx
    from mayday.agent.gateway import OllamaGateway
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            lines = [{"message": {"content": "thinking..."}}, {"done": True}]
        else:
            lines = [{"message": {"tool_calls": [{"function": {"name": "traffic", "arguments": "{'window': 'incident',}"}}]}}, {"done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
    g = OllamaGateway(client=httpx.Client(transport=httpx.MockTransport(handler)))
    from mayday.agent.tools import specs
    r = g.chat([{"role": "user", "content": "hi"}], specs(["traffic"]), require_tool=True)
    assert r.tool_calls[0].name == "traffic" and r.tool_calls[0].args == {"window": "incident"} and len(calls) == 2
    assert g.status() == {"backend": "ollama", "name": "gemma4:e4b", "healthy": True, "warm": False}


def test_circuit_breaker_opens():
    import httpx
    from mayday.agent.gateway import OllamaGateway, ModelError, ModelUnavailable
    g = OllamaGateway(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    for _ in range(3):
        with pytest.raises(ModelError):
            g.chat([{"role": "user", "content": "x"}])
    with pytest.raises(ModelUnavailable):
        g.chat([{"role": "user", "content": "x"}])
    assert g.status()["healthy"] is False


def test_make_gateway_falls_back(monkeypatch):
    from mayday.agent import make_gateway
    monkeypatch.setenv("MAYDAY_MODEL_URL", "http://127.0.0.1:9")
    assert make_gateway().status()["backend"] == "scripted"
