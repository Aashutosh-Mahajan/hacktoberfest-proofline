import time

from fastapi.testclient import TestClient

from mayday.api.replay import Recorder
from conftest import StubOrch, make_app, wait_for


def _drive_incident(hub, scenario="S1", seed=1, sec=40):
    hub.start_scenario(scenario, seed)
    for _ in range(sec):
        hub.sim.advance(1000)
        time.sleep(0.005)


def test_s0_run_is_not_recorded(client):
    client.hub.sim.advance(20_000)
    assert client.get("/api/replay/list").json()["runs"] == []


def test_incident_run_recorded_and_listed(client, runs_dir):
    _drive_incident(client.hub)
    r = client.get("/api/replay/list").json()
    assert r["ok"] and len(r["runs"]) == 1
    run = r["runs"][0]
    assert set(run) >= {"run_id", "scenario", "started", "duration_s", "events"}
    assert run["scenario"] == "S1" and run["events"] > 100 and run["duration_s"] > 0
    assert (runs_dir / f"{run['run_id']}.jsonl").exists()


def test_keep_last_10(tmp_path):
    rec = Recorder(tmp_path / "r", keep=10)
    for i in range(13):
        rec.begin("S1", i, 1.0, 15)
        rec.on_event({"v": 2, "seq": 1, "ts": 1, "type": "incident.opened", "data": {}})
        rec.on_event({"v": 2, "seq": 2, "ts": 2, "type": "metrics.tick", "data": {}})
        time.sleep(1.05 if i % 4 == 3 else 0.0)   # run ids are second-resolution + random suffix
    rec.close()
    assert len(list((tmp_path / "r").glob("run_*.jsonl"))) == 10
    assert len(rec.list_runs()) == 10


def test_replay_roundtrip_same_ws_and_timing(tmp_path):
    clk = {"t": 0.0}
    app = make_app(tmp_path)
    hub = app.state.hub
    hub.recorder.clock = lambda: clk["t"]       # deterministic wall offsets: 0.1 s per simulated second
    with TestClient(app) as c:
        hub.start_scenario("S1", 3)
        clk["t"] = 0.0
        hub.recorder._t0 = 0.0
        for _ in range(40):
            clk["t"] += 0.1
            hub.sim.advance(1000)
        orig = hub.bus.since(0)
        listed = c.get("/api/replay/list").json()["runs"][0]
        assert abs(listed["duration_s"] - 4.0) < 0.15
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()                                   # snapshot
            t0 = time.monotonic()
            r = c.post("/api/replay/start", json={"run_id": listed["run_id"], "speed": 4.0}).json()
            assert r["ok"] and r["events"] > 100
            got, stamps = [], []
            while len(got) < r["events"] + 1 and time.monotonic() - t0 < 20:
                m = ws.receive_json()
                if m["type"] == "snapshot":
                    continue
                got.append(m)
                stamps.append(time.monotonic() - t0)
        # original sim timestamps preserved, same event order for the run's content
        assert [e["ts"] for e in got if e["type"] == "incident.opened"] == \
               [e["ts"] for e in orig if e["type"] == "incident.opened"]
        assert [e["type"] for e in got[-30:]] == [e["type"] for e in orig if e["type"] != "snapshot"][-30:]
        # timing: 4.0 s of original timeline at 4x -> ~1 s wall
        assert 0.7 < stamps[-1] < 2.5, stamps[-1]
        # incident.opened arrives later than the first metrics (relative spacing kept)
        i_open = next(i for i, e in enumerate(got) if e["type"] == "incident.opened")
        assert stamps[i_open] > 0.2
        st = c.get("/api/state").json()
        assert st["sim"]["replay"] is True and st["sim"]["running"] is False
        assert c.post("/api/sim/control", json={"speed": 5}).status_code == 409
        assert c.post("/api/replay/stop").json()["ok"]
        st = c.get("/api/state").json()
        assert st["sim"]["replay"] is False and "incident" not in st


def test_replay_suppresses_agent_and_recording(tmp_path):
    calls = []

    class O(StubOrch):
        def on_incident(self, inc):
            calls.append(inc["id"])

    app = make_app(tmp_path, factory=lambda w, b: O(w, b))
    hub = app.state.hub
    with TestClient(app) as c:
        _drive_incident(hub)
        assert wait_for(lambda: calls == ["inc_0001"])
        run_id = c.get("/api/replay/list").json()["runs"][0]["run_id"]
        assert c.post("/api/replay/start", json={"run_id": run_id, "speed": 500}).json()["ok"]
        assert wait_for(lambda: not hub.replayer.playing)
        time.sleep(0.2)
        assert calls == ["inc_0001"]                                # replayed incident.opened did not start the agent
        assert len(c.get("/api/replay/list").json()["runs"]) == 1   # replay itself is not re-recorded
        c.post("/api/replay/stop")


def test_replay_errors(client):
    assert client.post("/api/replay/start", json={"run_id": "nope"}).json()["ok"] is False
    assert client.post("/api/replay/start", json={"run_id": "../../etc/passwd"}).json()["ok"] is False
    assert client.post("/api/replay/start", json={}).status_code == 422
    assert client.post("/api/replay/stop").json()["ok"]


def test_scenario_during_replay_stops_it(client):
    _drive_incident(client.hub)
    rid = client.get("/api/replay/list").json()["runs"][0]["run_id"]
    client.post("/api/replay/start", json={"run_id": rid, "speed": 1})
    assert client.hub.replaying
    assert client.post("/api/scenario", json={"scenario": "S2", "seed": 2}).json()["ok"]
    assert not client.hub.replaying and client.get("/api/state").json()["sim"]["replay"] is False


def test_chaos_run_hidden_in_list_until_revealed(client):
    hub = client.hub
    client.post("/api/chaos")
    hub.sim.advance(60_000)
    runs = client.get("/api/replay/list").json()["runs"]
    assert runs and runs[0]["scenario"] is None and runs[0]["chaos"] is True
    hub.bus.emit("rca.ready", {"rca": {"cause": "x", "incident_id": "inc_0001"}})
    runs = client.get("/api/replay/list").json()["runs"]
    assert runs[0]["scenario"] in ("S1", "S2", "S3", "S5")


def test_evidence_and_bulletin_replay(tmp_path):
    ev = {}

    class O(StubOrch):
        def get_evidence(self, eid):
            return ev.get(eid)

    app = make_app(tmp_path, factory=lambda w, b: O(w, b))
    hub = app.state.hub
    with TestClient(app) as c:
        hub.start_scenario("S2", 2)
        hub.sim.advance(50_000)
        b = hub.sim.telemetry.read_bulletin("BANK_X")
        assert b
        ev["ev_bulletin_BANK_X"] = {"id": "ev_bulletin_BANK_X", "kind": "bulletin", "title": "Bulletin", "ts": 1,
                                    "body": {"bulletin": {"image_url": b["image_url"], "extracted": b["extracted"],
                                                          "consistent": True, "detail": "d"}}}
        hub.bus.emit("agent.tool_result", {"incident_id": "inc_0001", "call_id": "c", "tool": "read_bulletin",
                                           "summary": "s", "ok": True, "evidence_ids": ["ev_bulletin_BANK_X"], "ms": 1})
        rid = c.get("/api/replay/list").json()["runs"][0]["run_id"]
        c.post("/api/replay/start", json={"run_id": rid, "speed": 500})
        ev.clear()                                                   # live store gone: must come from the recording
        assert c.get("/api/evidence/ev_bulletin_BANK_X").json()["kind"] == "bulletin"
        r = c.get("/api/bulletin/BANK_X.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        c.post("/api/replay/stop")
