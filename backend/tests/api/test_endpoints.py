import io
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from conftest import StubOrch, make_app, wait_for


def _wav(seconds, rate=8000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return b.getvalue()


def events(c, typ):
    return [e for e in c.hub.bus.since(0) if e["type"] == typ]


# ---------------------------------------------------------------- model / health
def test_health_reports_live_model_and_installed_models(stub_client):
    h = stub_client.get("/api/health").json()
    assert h["model"]["name"] == "stub-gemma" and h["agent_loaded"] and h["models"] == ["gemma4:e4b", "gemma4:e2b"]
    r = stub_client.post("/api/model", json={"model": "gemma4:e2b"}).json()
    assert r["ok"] and r["model"]["name"] == "gemma4:e2b"
    assert stub_client.get("/api/health").json()["model"]["name"] == "gemma4:e2b"      # live, not cached
    assert stub_client.get("/api/state").json()["model"]["name"] == "gemma4:e2b"


def test_model_errors(stub_client, client):
    r = stub_client.post("/api/model", json={"model": "llama-70b"})
    assert r.status_code == 400 and r.json()["ok"] is False
    assert stub_client.post("/api/model", json={}).status_code == 422
    assert stub_client.post("/api/model", json={"model": ""}).json()["ok"] is False
    r = client.post("/api/model", json={"model": "gemma4:e4b"})        # no agent
    assert r.status_code == 501 and r.json()["ok"] is False
    assert "models" not in client.get("/api/health").json()


def test_model_unsupported_orchestrator(tmp_path):
    app = make_app(tmp_path, factory=lambda w, b: StubOrch(w, b, with_set_model=False))
    with TestClient(app) as c:
        r = c.post("/api/model", json={"model": "x"})
        assert r.status_code == 501 and r.json()["ok"] is False


# ---------------------------------------------------------------- ask
@pytest.mark.parametrize("text,lang", [("BANK_Y pe koi asar hua?", "hi"), ("Is BANK_X healthy?", "en"),
                                       ("बैंक Y पर कोई असर हुआ?", None)])
def test_ask_text_events(stub_client, text, lang):
    body = {"text": text}
    if lang:
        body["lang"] = lang
    r = stub_client.post("/api/ask", json=body).json()
    assert r["ok"] and r["query_id"]
    qid = r["query_id"]
    assert wait_for(lambda: [e for e in events(stub_client, "voice.answer") if e["data"]["query_id"] == qid])
    q = [e for e in events(stub_client, "voice.query") if e["data"]["query_id"] == qid][0]
    assert q["data"]["transcript"] == text


def test_ask_validation(stub_client, client):
    assert stub_client.post("/api/ask", json={"text": "   "}).json()["ok"] is False
    assert stub_client.post("/api/ask", json={}).status_code == 422
    assert stub_client.post("/api/ask", json={"text": "x" * 5000}).status_code == 422
    assert stub_client.post("/api/ask", json={"text": 5}).status_code == 422
    assert stub_client.post("/api/ask", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/api/ask", json={"text": "hi"}).status_code == 503


# ---------------------------------------------------------------- voice
def test_voice_ok(stub_client):
    r = stub_client.post("/api/voice", files={"audio": ("a.webm", b"\x1aE\xdf\xa3" + b"0" * 4000, "audio/webm")}).json()
    assert r["ok"] and r["query_id"] and "fallback" not in r
    assert stub_client.hub.orch.asked[-1][0].startswith("<audio 4004B audio/webm")


def test_voice_wav_duration_limit(stub_client):
    ok = stub_client.post("/api/voice", files={"audio": ("a.wav", _wav(29), "audio/wav")})
    assert ok.json()["ok"]
    long = stub_client.post("/api/voice", files={"audio": ("a.wav", _wav(31), "audio/wav")})
    assert long.status_code == 413 and long.json()["ok"] is False
    big = stub_client.post("/api/voice", files={"audio": ("a.webm", b"0" * (3 * 1024 * 1024), "audio/webm")})
    assert big.status_code == 413
    d = stub_client.post("/api/voice", files={"audio": ("a.webm", b"0" * 100, "audio/webm")}, data={"duration_s": "45"})
    assert d.status_code == 413
    assert stub_client.post("/api/voice", files={"audio": ("a.webm", b"", "audio/webm")}).json()["ok"] is False
    assert stub_client.post("/api/voice").status_code == 422
    assert stub_client.post("/api/voice", files={"audio": ("a.wav", b"RIFF" + b"0" * 4 + b"WAVEgarbage", "audio/wav")}).json()["ok"] is False


def test_voice_audio_unsupported_falls_back(tmp_path):
    app = make_app(tmp_path, factory=lambda w, b: StubOrch(w, b, audio=False))
    with TestClient(app) as c:
        c.hub = app.state.hub
        r = c.post("/api/voice", files={"audio": ("a.webm", b"0" * 500, "audio/webm")})
        j = r.json()
        assert r.status_code == 200 and j["ok"] and j["query_id"] and j["fallback"] == "audio_unsupported" and "/api/ask" in j["hint"]
        err = [e for e in events(c, "error") if e["data"]["code"] == "audio_unsupported"]
        assert err and err[0]["data"]["recoverable"] is True


def test_voice_without_agent(client):
    assert client.post("/api/voice", files={"audio": ("a.webm", b"0" * 10, "audio/webm")}).status_code == 503


# ---------------------------------------------------------------- never 500 on bad input
BAD = [
    ("/api/scenario", {"scenario": "S9"}), ("/api/scenario", {"scenario": "S1", "seed": -4}),
    ("/api/scenario", {"scenario": "S1", "severity": 0}), ("/api/scenario", {"scenario": "S1", "severity": 1e9}),
    ("/api/scenario", {"scenario": "S1", "fault_offset_s": -50}), ("/api/scenario", {"scenario": "S1", "seed": "abc"}),
    ("/api/scenario", {"scenario": None}), ("/api/scenario", []), ("/api/scenario", "str"),
    ("/api/approve", {}), ("/api/approve", {"incident_id": 1, "action_id": None}),
    ("/api/reject", {"incident_id": "x"}), ("/api/comms/approve", {}),
    ("/api/sim/control", {"speed": -1}), ("/api/sim/control", {"speed": 1e9}), ("/api/sim/control", {"advance_s": 1e9}),
    ("/api/sim/control", {"speed": "fast"}), ("/api/replay/start", {"run_id": 5}), ("/api/replay/start", {"run_id": "x", "speed": 0}),
    ("/api/model", {"model": 5}), ("/api/model", []), ("/api/ask", {"text": None}),
]


@pytest.mark.parametrize("path,body", BAD)
def test_bad_input_never_500(stub_client, path, body):
    r = stub_client.post(path, json=body)
    assert r.status_code < 500, (path, body, r.text)
    assert r.json()["ok"] is False


def test_bad_routes_and_methods(stub_client):
    for r in (stub_client.get("/api/nope"), stub_client.get("/api/scenario"), stub_client.get("/api/evidence/%00"),
              stub_client.get("/api/evidence/ev_deploy_xyz"), stub_client.get("/api/evidence/ev_deploy_99999999999999999999"),
              stub_client.get("/api/bulletin/NOPE.png"), stub_client.get("/api/bulletin/BANK_X.png"),
              stub_client.post("/api/scenario", content=b"\xff\xfe", headers={"content-type": "application/json"})):
        assert r.status_code < 500 and r.json()["ok"] is False, r.url


def test_sim_control_works_and_valid(stub_client):
    r = stub_client.post("/api/sim/control", json={"speed": 2.5, "advance_s": 3}).json()
    assert r["ok"] and r["status"]["speed"] == 2.5 and r["status"]["sim_ms"] > 0


# ---------------------------------------------------------------- follow-ups (next steps)
class FollowOrch(StubOrch):
    def followup(self, incident_id, kind):
        if kind == "escalate_bank" and self.fail:
            return {"ok": False, "error": "no bank-side cause"}
        self.bus.emit("followup.ready", {"incident_id": incident_id, "kind": kind, "title": f"T {kind}",
                                         "body": "## md", "audience": "Engineering", "simulated": True})
        return {"ok": True}

    fail = False


def _follow_client(tmp_path):
    app = make_app(tmp_path, factory=lambda w, b: FollowOrch(w, b))
    return app


def test_followup_event_snapshot_replay_and_clear(tmp_path):
    app = _follow_client(tmp_path)
    with TestClient(app) as c:
        hub = app.state.hub
        hub.start_scenario("S1", 1)
        hub.sim.advance(40_000)
        iid = hub.tracker.incident["id"]
        assert c.post("/api/followup", json={"incident_id": iid, "kind": "permanent_fix"}).json() == {"ok": True}
        assert c.post("/api/followup", json={"incident_id": iid, "kind": "permanent_fix"}).json()["ok"]    # re-draft replaces
        assert c.post("/api/followup", json={"incident_id": iid, "kind": "followup_tasks"}).json()["ok"]
        ev = [e for e in hub.bus.since(0) if e["type"] == "followup.ready"]
        assert len(ev) == 3 and ev[0]["data"]["simulated"] is True
        fu = c.get("/api/state").json()["followups"]
        assert sorted(f["kind"] for f in fu) == ["followup_tasks", "permanent_fix"]
        with c.websocket_connect("/ws") as ws:
            assert len(ws.receive_json()["data"]["followups"]) == 2           # reload keeps them
        # recorded and replayed
        hub.sim.advance(5000)
        rid = c.get("/api/replay/list").json()["runs"][0]["run_id"]
        c.post("/api/replay/start", json={"run_id": rid, "speed": 1000})
        assert wait_for(lambda: not hub.replayer.playing)
        assert sorted(f["kind"] for f in c.get("/api/state").json()["followups"]) == ["followup_tasks", "permanent_fix"]
        assert c.post("/api/followup", json={"incident_id": iid, "kind": "permanent_fix"}).status_code == 409
        c.post("/api/replay/stop")
        assert "followups" not in c.get("/api/state").json()                   # cleared on new scenario/reset
        hub.start_scenario("S1", 1); hub.sim.advance(40_000)
        c.post("/api/followup", json={"incident_id": hub.tracker.incident["id"], "kind": "merchant_notice"})
        c.post("/api/reset")
        assert "followups" not in c.get("/api/state").json()


def test_followup_validation(tmp_path, client):
    app = _follow_client(tmp_path / "x")
    with TestClient(app) as c:
        hub = app.state.hub
        assert c.post("/api/followup", json={"incident_id": "inc_0001", "kind": "permanent_fix"}).status_code == 404   # none open
        hub.start_scenario("S1", 1); hub.sim.advance(40_000)
        iid = hub.tracker.incident["id"]
        assert c.post("/api/followup", json={"incident_id": iid, "kind": "bogus"}).status_code == 422
        assert c.post("/api/followup", json={"incident_id": "inc_9999", "kind": "permanent_fix"}).status_code == 404
        assert c.post("/api/followup", json={"kind": "permanent_fix"}).status_code == 422
        hub.orch.fail = True
        r = c.post("/api/followup", json={"incident_id": iid, "kind": "escalate_bank"})
        assert r.status_code == 400 and r.json() == {"ok": False, "error": "no bank-side cause"}
    r = client.post("/api/followup", json={"incident_id": "x", "kind": "permanent_fix"})      # no agent
    assert r.status_code == 501 and r.json()["ok"] is False


def test_followup_unsupported_orchestrator(stub_client):
    r = stub_client.post("/api/followup", json={"incident_id": "inc_0001", "kind": "permanent_fix"})
    assert r.status_code == 501 and r.json()["ok"] is False
