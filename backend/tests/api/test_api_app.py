import pytest
from fastapi.testclient import TestClient

from mayday.api.app import build_app


@pytest.fixture()
def client():
    app = build_app(autostart=False, use_agent=False)
    with TestClient(app) as c:
        c.hub = app.state.hub
        yield c


def test_health_state_and_ws_snapshot(client):
    assert client.get("/api/health").json()["contract_version"] == 2
    assert "rows" in client.get("/api/eval/latest").json()
    with client.websocket_connect("/ws") as ws:
        m = ws.receive_json()
        assert m["type"] == "snapshot" and m["data"]["contract_version"] == 2
        client.hub.sim.advance(2000)
        assert ws.receive_json()["type"] in ("metrics.tick", "sim.status")


def test_scenario_incident_and_resume(client):
    assert client.post("/api/scenario", json={"scenario": "S1", "seed": 1}).json()["ok"]
    client.hub.sim.advance(60_000)
    types = [e["type"] for e in client.hub.bus.since(0)]
    assert "incident.opened" in types and "deploy.event" in types
    with client.websocket_connect("/ws?since=0") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["data"]["incident"]["id"] == "inc_0001"
    assert client.get("/api/evidence/ev_deploy_482").json()["kind"] == "deploy"
    assert client.post("/api/reset").json()["ok"]


def test_agent_absent_and_chaos(client):
    assert client.post("/api/approve", json={"incident_id": "x", "action_id": "y", "token": "t"}).status_code == 503
    r = client.post("/api/chaos").json()
    assert r["ok"] and r["round"] == 1
    st = client.get("/api/state").json()
    assert st["sim"]["chaos_hidden"] and st["sim"]["scenario"] is None
    gt = client.hub.sim.world.ground_truth()
    client.hub.bus.emit("rca.ready", {"rca": {"cause": gt["cause"], "incident_id": "inc_0001"}})
    ev = [e for e in client.hub.bus.since(0) if e["type"] == "chaos.revealed"]
    assert ev and ev[0]["data"]["correct"] is True
    assert client.get("/api/state").json()["scoreboard"][0]["round"] == 1


def test_bulletin_png(client):
    client.post("/api/scenario", json={"scenario": "S2", "seed": 2})
    client.hub.sim.advance(50_000)
    r = client.get("/api/bulletin/BANK_X.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
