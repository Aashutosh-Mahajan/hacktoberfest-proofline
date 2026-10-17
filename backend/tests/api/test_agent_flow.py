"""End-to-end API tests with the real Orchestrator driven by the project's fake gateway (no Ollama)."""
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import fake_gateway, make_app, wait_for
from mayday.agent.orchestrator import Orchestrator

pytestmark = pytest.mark.skipif(fake_gateway() is None, reason="tests/agent/fake_gateway.py not available")


def make(tmp_path, latency_s=0.0):
    gw = fake_gateway(latency_s=latency_s)
    return make_app(tmp_path, factory=lambda w, b: Orchestrator(w, b, gw, model_timeout_s=10, total_budget_s=60,
                                                                 poll_s=0.05))


def run_until(hub, pred, max_sim_s=150, step_ms=1000, wall_s=40):
    end = time.time() + wall_s
    n = 0
    while time.time() < end and n < max_sim_s:
        hub.sim.advance(step_ms)
        n += 1
        time.sleep(0.03)
        if pred():
            return True
    return False


def evs(hub, typ=None):
    return [e for e in hub.bus.since(0) if typ is None or e["type"] == typ]


SCEN_RE = re.compile(r"(?<![A-Za-z0-9])S[0-6](?![A-Za-z0-9])")


@pytest.mark.parametrize("round_", range(4))
def test_chaos_arm_run_reveal_and_no_scenario_leak(tmp_path, round_):
    app = make(tmp_path)
    with TestClient(app) as c:
        hub = app.state.hub
        assert c.post("/api/chaos").json()["round"] == 1
        st = c.get("/api/state").json()
        assert st["sim"]["chaos_hidden"] is True and st["sim"]["scenario"] is None
        assert run_until(hub, lambda: evs(hub, "chaos.revealed"), wall_s=60), "no reveal"
        rev = evs(hub, "chaos.revealed")[0]["data"]
        assert rev["scenario"] in ("S1", "S2", "S3", "S5") and rev["round"] == 1
        assert rev["correct"] is True, rev                      # fake gateway solves the four chaos scenarios
        sc = c.get("/api/state").json()["scoreboard"]
        assert sc and sc[0]["correct"] is True and sc[0]["scenario"] == rev["scenario"]
        assert c.get("/api/state").json()["sim"]["scenario"] == rev["scenario"]   # un-hidden after reveal
        # ---- leak audit: nothing before the reveal may contain the scenario id
        armed = evs(hub, "chaos.armed")[0]["seq"]
        seq_rev = next(e["seq"] for e in evs(hub, "sim.status") if e["seq"] > armed and not e["data"]["chaos_hidden"])
        before = [e for e in hub.bus.since(0) if e["seq"] < seq_rev]
        assert before
        for e in before:
            blob = json.dumps(e)
            assert not SCEN_RE.search(blob), (e["type"], SCEN_RE.search(blob).group(0), blob[:200])
        assert not SCEN_RE.search(json.dumps(c.get("/api/health").json()))
        runs = c.get("/api/replay/list").json()["runs"]
        assert runs and runs[0]["scenario"] == rev["scenario"]    # revealed -> visible


def test_chaos_hidden_state_while_armed(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as c:
        hub = app.state.hub
        c.post("/api/chaos")
        for _ in range(5):
            hub.sim.advance(1000)
            s = c.get("/api/state").json()
            assert s["sim"]["scenario"] is None and s["sim"]["chaos_hidden"] and s["sim"]["fault_active"] is False
        for e in evs(hub, "sim.status"):
            assert e["data"]["scenario"] is None
        with c.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["data"]["sim"]["scenario"] is None


def test_scoreboard_accumulates_over_rounds(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as c:
        hub = app.state.hub
        for rnd in (1, 2):
            assert c.post("/api/chaos").json()["round"] == rnd
            assert run_until(hub, lambda: len(c.get("/api/state").json()["scoreboard"]) == rnd, wall_s=60)
        assert [r["round"] for r in c.get("/api/state").json()["scoreboard"]] == [1, 2]


def test_scenario_switch_mid_agent_run_leaks_no_stale_events(tmp_path):
    app = make(tmp_path, latency_s=0.25)               # slow model so the agent is mid-run at the switch
    with TestClient(app) as c:
        hub = app.state.hub
        c.post("/api/scenario", json={"scenario": "S1", "seed": 1, "fault_offset_s": 12})
        assert run_until(hub, lambda: [e for e in evs(hub, "agent.tool_call")], wall_s=30)
        assert hub.orch._thread.is_alive()
        old_cause = None
        c.post("/api/scenario", json={"scenario": "S5", "seed": 4, "fault_offset_s": 12})
        # right after the switch the new run's bus is clean; the old agent must not talk into it
        t_end = time.time() + 1.5
        while time.time() < t_end:
            time.sleep(0.05)
        stale = [e for e in evs(hub) if e["type"].startswith(("agent.", "hypothesis.", "verify.", "rca.", "twin.", "action."))]
        assert stale == [], [e["type"] for e in stale]
        # new run: incident opens, agent runs, and every agent event belongs to the new incident / S5 truth
        assert run_until(hub, lambda: evs(hub, "rca.ready"), wall_s=40)
        opened = evs(hub, "incident.opened")
        assert len(opened) == 1
        first = opened[0]["seq"]
        for e in evs(hub):
            if e["type"].startswith(("agent.", "hypothesis.", "verify.", "rca.", "twin.")):
                assert e["seq"] > first
                blob = json.dumps(e)
                assert "deploy:482" not in blob and "#482" not in blob, e["type"]   # S1's cause never reaches the S5 run
        rca = evs(hub, "rca.ready")[0]["data"]["rca"]
        assert rca["cause"].startswith("business")


def test_reset_mid_run_is_clean(tmp_path):
    app = make(tmp_path, latency_s=0.2)
    with TestClient(app) as c:
        hub = app.state.hub
        c.post("/api/scenario", json={"scenario": "S2", "seed": 2, "fault_offset_s": 12})
        assert run_until(hub, lambda: evs(hub, "agent.tool_call"), wall_s=30)
        assert c.post("/api/reset").json()["ok"]
        time.sleep(1.0)
        assert [e for e in evs(hub) if e["type"].startswith(("agent.", "rca.", "hypothesis."))] == []
        st = c.get("/api/state").json()
        assert st["agent_state"] == "idle" and "incident" not in st


def test_ask_real_orchestrator_english_and_hindi(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as c:
        hub = app.state.hub
        c.post("/api/scenario", json={"scenario": "S1", "seed": 1, "fault_offset_s": 12})
        hub.sim.advance(40_000)
        out = {}
        for text, lang in (("Is BANK_X healthy right now?", "en"), ("BANK_Y pe koi asar hua?", "hi")):
            r = c.post("/api/ask", json={"text": text, "lang": lang}).json()
            assert r["ok"]
            out[r["query_id"]] = text
        assert wait_for(lambda: len(evs(hub, "voice.answer")) == 2, timeout=20)
        for q in evs(hub, "voice.query"):
            assert out[q["data"]["query_id"]] == q["data"]["transcript"]
        for a in evs(hub, "voice.answer"):
            assert a["data"]["text"] and a["data"]["query_id"] in out
