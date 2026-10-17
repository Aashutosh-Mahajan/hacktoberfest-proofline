"""Gateway robustness with a mocked Ollama transport (JSON repair, re-ask, text-form calls, timeouts, honest status)."""
import json
import time

import httpx
import pytest

from mayday.agent.compact import compact_messages, est_tokens
from mayday.agent.gateway import ModelError, ModelTimeout, ModelUnavailable, OllamaGateway, make_gateway
from mayday.agent.orchestrator import SYSTEM_PROMPT
from mayday.agent.tools import SLIM_SPECS, TELEMETRY_TOOLS, CONTROL_TOOLS, specs


def gw(handler):
    return OllamaGateway(client=httpx.Client(transport=httpx.MockTransport(handler)))


def ndjson(*chunks):
    return httpx.Response(200, content="\n".join(json.dumps(c) for c in chunks).encode())


def call_chunk(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


FULL = specs(TELEMETRY_TOOLS + CONTROL_TOOLS)
U = [{"role": "user", "content": "x"}]


def test_request_is_lean_and_think_false():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return ndjson(call_chunk("bank_stats", {}), {"done": True, "prompt_eval_count": 7, "eval_count": 3})
    g = gw(h)
    r = g.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "CONTEXT:\n{}"}], FULL, require_tool=True)
    b = seen[0]
    assert b["think"] is False and b["options"]["num_predict"] <= 700 and b["stream"] is True
    names = {t["function"]["name"] for t in b["tools"]}
    assert names == set(SLIM_SPECS) & {t["function"]["name"] for t in FULL}
    assert "update_hypothesis" not in names and "preview_fix" not in names
    assert len(json.dumps(b["tools"])) < 2200
    assert est_tokens(SYSTEM_PROMPT) < 520
    assert r.stats["prompt_tokens"] == 7 and r.via == "gemma4:e4b"


def test_parallel_tool_calls_and_arg_repair():
    def h(req):
        return ndjson({"message": {"tool_calls": [
            {"function": {"name": "bank_stats", "arguments": {}}},
            {"function": {"name": "submit_conclusion", "arguments": {"cause": "deploy:1", "ruled_out": '[{"target": "x", "reason": "y",}]'}}},
            {"function": {"name": "traffic", "arguments": "{'window': 'incident',}"}}]}}, {"done": True})
    r = gw(h).chat(U, FULL, require_tool=True)
    assert [c.name for c in r.tool_calls] == ["bank_stats", "submit_conclusion", "traffic"]
    assert r.tool_calls[1].args["ruled_out"] == [{"target": "x", "reason": "y"}]
    assert r.tool_calls[2].args == {"window": "incident"}


@pytest.mark.parametrize("text", ['{"name": "traffic", "arguments": {"window": "incident"}}',
                                  '```json\n[{"name":"traffic","arguments":{"window":"incident"}}]\n```',
                                  "traffic(window='incident')", 'call:traffic{"window": "incident"}'])
def test_text_form_tool_calls_are_recovered(text):
    r = gw(lambda req: ndjson({"message": {"content": text}}, {"done": True})).chat(U, FULL, require_tool=True)
    assert r.tool_calls and r.tool_calls[0].name == "traffic" and r.tool_calls[0].args == {"window": "incident"}


def test_reask_once_then_gives_up():
    n = []

    def h(req):
        n.append(json.loads(req.content))
        return ndjson({"message": {"content": "I think the bank is down."}}, {"done": True})
    r = gw(h).chat(U, FULL, require_tool=True)
    assert len(n) == 2 and not r.tool_calls and r.stats["reasked"]
    assert "tool call" in n[1]["messages"][-1]["content"]


def test_timeout_raises_modeltimeout_and_trips_breaker():
    def h(req):
        raise httpx.ReadTimeout("slow")
    g = gw(h)
    for _ in range(3):
        with pytest.raises(ModelTimeout):
            g.chat(U, FULL)
    with pytest.raises(ModelUnavailable):
        g.chat(U, FULL)
    assert g.status()["healthy"] is False


def test_unreachable_server_status_is_honest(monkeypatch):
    monkeypatch.setenv("MAYDAY_MODEL_URL", "http://127.0.0.1:9")
    g = make_gateway()
    st = g.status()
    assert st["backend"] == "ollama" and not st["healthy"] and "unavailable" in st["name"].lower()
    with pytest.raises(ModelError):
        g.chat(U, FULL)


def test_think_unsupported_model_retries_without_think():
    bodies = []

    def h(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "think" in b:
            return httpx.Response(400, content=b'{"error": "model does not support thinking"}')
        return ndjson(call_chunk("traffic", {}), {"done": True})
    r = gw(h).chat(U, FULL)
    assert r.tool_calls and "think" not in bodies[-1]


def test_set_model_validates_against_installed():
    def h(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}, {"name": "gemma4:e2b"}]})
        return httpx.Response(404)
    g = gw(h)
    assert g.set_model("gemma4:e2b")["name"] == "gemma4:e2b"
    with pytest.raises(ValueError):
        g.set_model("nope:1b")


def test_vision_reads_png_and_cross_checks_metadata(tmp_path):
    png = tmp_path / "b.png"
    png.write_bytes(b"\x89PNG fake")
    ref = 1_760_000_400_000
    meta = {"bank": "BANK_X", "status": "degraded", "claimed_start_ts": 1_760_000_280_000, "claimed_end_ts": 1_760_000_760_000}
    info = {"png_path": str(png), "extracted": meta}
    sent = []

    def mk(obj):
        def h(req):
            sent.append(json.loads(req.content))
            return ndjson({"message": {"content": json.dumps(obj)}}, {"done": True})
        return h

    def hms(ms):
        return time.strftime("%H:%M:%S", time.gmtime(ms // 1000))
    good = {"bank": "BANK_X", "status": "degraded", "start": hms(meta["claimed_start_ts"]), "end": hms(meta["claimed_end_ts"])}
    ex, note = gw(mk(good)).read_bulletin(info, "BANK_X", ref)
    assert ex == meta and "vision" in note and sent[0]["messages"][0]["images"] and sent[0]["format"]["required"]
    ex, note = gw(mk(dict(good, end="00:00:01"))).read_bulletin(info, "BANK_X", ref)
    assert ex == meta and "misread" in note
    with pytest.raises(ModelError):
        gw(lambda r: httpx.Response(500)).read_bulletin(info, "BANK_X", ref)


def test_bulletin_tool_falls_back_to_metadata_and_says_so(tmp_path):
    from fakes import FakeWorld
    from mayday.agent.evidence import EvidenceStore
    from mayday.agent.tools import Toolbox
    from mayday.agent.util import NormWorld
    w = NormWorld(FakeWorld("S2"))
    tb = Toolbox(w, EvidenceStore(), lambda: w.incident(), gw(lambda r: httpx.Response(500)))
    r = tb.call("read_bulletin", {"bank": "x"})
    assert r.ok and "metadata" in r.summary or "metadata" in r.data["detail"]


def test_audio_unsupported_has_clear_error():
    g = gw(lambda req: httpx.Response(200, json={"capabilities": ["completion", "tools", "vision"]}))
    assert not g.supports_audio()
    g2 = gw(lambda req: httpx.Response(400, content=b'{"error": {"message": "audio not supported"}}'))
    with pytest.raises(ModelError):
        g2.transcribe(b"RIFFxxxx", "audio/wav")


def test_compact_messages_terse_and_collapses_old_results():
    big = {"ok": True, "summary": "s" * 50, "data": {"banks": {"BANK_X": {"tsr": 0.5, "base_tsr": 0.99, "d_pp": -49, "p95": 800, "base_p95": 400, "err5xx_pct": 0.1, "tps": 100}}}, "evidence_ids": ["ev_bank_BANK_X"]}
    ctx = {"incident": {"id": "i"}, "triage": {}, "hypotheses": [{"id": "h1", "target": "deploy:1"}]}
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "CONTEXT:\n" + json.dumps(ctx)},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "bank_stats", "arguments": {}}}]},
            {"role": "tool", "tool_call_id": "a", "tool_name": "bank_stats", "content": json.dumps(big)},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {"name": "traffic", "arguments": {}}}]},
            {"role": "tool", "tool_call_id": "b", "tool_name": "traffic", "content": json.dumps({"ok": True, "summary": "t", "data": {"ratio": 1, "baseline_tps": 3}, "evidence_ids": []})}]
    out = compact_messages(msgs)
    assert "BANK_X tsr=0.5" in out[3]["content"] and "h1 = deploy:1" in out[1]["content"]
    tiny = compact_messages(msgs, budget_tokens=10)
    assert "tsr=" not in tiny[3]["content"] and "ev_bank_BANK_X" in tiny[3]["content"]
    assert "tool_call_id" not in out[2] and "tool_call_id" not in out[3]


def test_audio_transcribe_via_openai_compat_input_audio():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"transcript": "bank X ka status?", "lang": "hinglish"}'}}]})
    r = gw(h).transcribe(b"RIFFxxxx", "audio/wav")
    assert r == {"transcript": "bank X ka status?", "lang": "hinglish"}
    assert seen[0]["messages"][0]["content"][1]["input_audio"]["format"] == "wav"
