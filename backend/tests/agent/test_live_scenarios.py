"""Opt-in acceptance checks: real Gemma, API approval and real simulator for every menu choice."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from mayday.agent.gateway import OllamaGateway
from mayday.agent.orchestrator import Orchestrator
from mayday.api.app import build_app


@pytest.mark.live
@pytest.mark.parametrize("scenario,expected_cause,expected_action", [
    ("S0", None, None),
    ("S1", "deploy:482", "rollback_deploy"),
    ("S2", "bank:BANK_X", "notify_only"),
    ("S3", "bank:BANK_X", "notify_only"),
    ("S4", "traffic_spike", "scale_gateway"),
    ("S5", "business:limit_exceeded", "notify_only"),
    ("S6", "deploy:482", "rollback_deploy"),
])
def test_live_menu_scenario(tmp_path, scenario, expected_cause, expected_action):
    gateway = OllamaGateway()
    app = build_app(autostart=False, use_agent=False, runs_dir=tmp_path / "runs",
                    orchestrator_factory=lambda w, b: Orchestrator(w, b, gateway, model_timeout_s=45,
                                                                   total_budget_s=150, poll_s=.01))
    with TestClient(app) as client:
        hub = app.state.hub
        assert client.post("/api/scenario", json={"scenario": scenario, "seed": 1,
                                                 "severity": 1, "fault_offset_s": 5}).json()["ok"]
        hub.sim.advance(90_000)
        if scenario == "S0":
            state = client.get("/api/state").json()
            assert "incident" not in state and "pending_action" not in state
            assert state["agent_state"] == "idle"
            print("\nSCENARIO RESULT:", json.dumps({"scenario": scenario, "result": "healthy; no false alarm"}))
            return
        deadline = time.monotonic() + 160
        while time.monotonic() < deadline:
            state = client.get("/api/state").json()
            if "pending_action" in state or state["agent_state"] in ("error", "inconclusive"):
                break
            time.sleep(.1)
        assert "pending_action" in state, state
        assert state["rca"]["cause"] == expected_cause, state["rca"]
        pending = state["pending_action"]
        action = pending["action"]
        assert action["kind"] == expected_action, action
        assert not any(e["type"] == "action.executed" for e in hub.bus.since(0))
        if expected_action != "notify_only":
            assert action["preview"]["recovers"]
        if scenario == "S3":
            assert any(r["target"] == "deploy:483" for r in state["rca"]["ruled_out"])
        if scenario == "S6":
            assert any("BANK_Y" in warning and "will not repair" in warning for warning in action["warnings"])
        assert client.post("/api/approve", json={"incident_id": pending["incident_id"],
            "action_id": action["id"], "token": pending["approval_token"]}).json()["ok"]
        execution = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            hub.sim.advance(250)
            execution = next((e["data"] for e in hub.bus.since(0) if e["type"] == "action.executed"), None)
            if execution:
                break
            time.sleep(.02)
        assert execution, hub.orch.snapshot()
        if expected_action == "notify_only":
            assert execution["recovered"] is False
            assert not any(e["type"] == "impact.savings" for e in hub.bus.since(0))
        else:
            assert execution["recovered"], execution
            assert execution["actual_recovery_ms"] > 0, execution
            assert hub.sim.world.success_rate_now(10) >= state["incident"]["baseline_success"] - 2
        print("\nSCENARIO RESULT:", json.dumps({"scenario": scenario, "cause": state["rca"]["cause"],
              "action": action["kind"], "source": state["rca"]["summary"], "approval": "passed",
              "recovered": execution["recovered"], "actual_recovery_ms": execution.get("actual_recovery_ms")}))
