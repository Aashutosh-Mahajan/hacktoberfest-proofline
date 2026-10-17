import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mayday.api.app import build_app

_AGENT_TESTS = str(Path(__file__).resolve().parents[1] / "agent")


class StubGateway:
    backend = "ollama"
    name = "stub-gemma"

    def __init__(self, audio=True):
        self.audio = audio

    def status(self):
        return {"backend": "ollama", "name": self.name, "healthy": True, "warm": True}

    def supports_audio(self):
        return self.audio


class StubOrch:
    """Minimal orchestrator surface used by the API (no investigation)."""

    def __init__(self, world, bus, audio=True, with_set_model=True):
        self.world, self.bus = world, bus
        self.gateway = StubGateway(audio)
        self.asked = []
        self.resets = 0
        self.models = ["gemma4:e4b", "gemma4:e2b"]
        if not with_set_model:
            self.set_model = None

    def list_models(self):
        return list(self.models)

    def set_model(self, name):
        if name not in self.models:
            raise ValueError(f"model {name!r} not installed")
        self.gateway.name = name
        return self.gateway.status()

    def get_evidence(self, eid):
        return None

    def reset(self):
        self.resets += 1

    def on_incident(self, inc):
        pass

    def approve(self, *a):
        return {"ok": False, "error": "no_pending_action"}

    reject = approve

    def ask(self, text, lang=None):
        qid = f"q_{len(self.asked):04d}"
        self.asked.append((text, lang))

        def go():
            self.bus.emit("voice.query", {"query_id": qid, "transcript": text, "lang": lang or "en"})
            self.bus.emit("voice.answer", {"query_id": qid, "text": "ok: " + text, "lang": lang or "en",
                                           "evidence_ids": []})
        threading.Thread(target=go, daemon=True).start()
        return qid

    def ask_audio(self, audio, mime="audio/webm"):
        qid = "q_audio"
        if not self.gateway.supports_audio():
            self.bus.emit("error", {"code": "audio_unsupported", "message": "no audio", "recoverable": True})
            return qid
        self.asked.append((f"<audio {len(audio)}B {mime}>", None))
        return qid


def make_app(tmp_path, factory=None, **kw):
    return build_app(autostart=False, use_agent=False, runs_dir=tmp_path / "runs",
                     orchestrator_factory=factory, **kw)


@pytest.fixture()
def runs_dir(tmp_path):
    return tmp_path / "runs"


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        c.hub = app.state.hub
        yield c


@pytest.fixture()
def stub_client(tmp_path):
    app = make_app(tmp_path, factory=lambda w, b: StubOrch(w, b))
    with TestClient(app) as c:
        c.hub = app.state.hub
        yield c


def wait_for(pred, timeout=10.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def fake_gateway(**kw):
    """The project's fake Gemma gateway (tests/agent/fake_gateway.py), or None if it is not available."""
    if _AGENT_TESTS not in sys.path:
        sys.path.insert(0, _AGENT_TESTS)
    try:
        import fake_gateway as fg
    except ImportError:
        return None
    for n in ("ScriptedGateway", "FakeGateway", "FakeGemma", "FakeGemmaGateway"):
        if hasattr(fg, n):
            return getattr(fg, n)(**kw)
    return None
