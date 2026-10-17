"""Orchestrator: bounded state machine TRIAGE -> INVESTIGATE -> CONCLUDE -> VERIFY -> PREVIEW -> PROPOSE ->
AWAIT_APPROVAL -> REMEDIATE -> COMMS -> POSTMORTEM. Model-agnostic; never raises into the caller."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from typing import Optional

from .comms import draft_comms
from .evidence import EvidenceStore
from .gateway import Gateway, ModelAborted, ModelError, ModelTimeout
from .postmortem import build_postmortem
from .tools import CONTROL_TOOLS, READ_ONLY_TOOLS, TELEMETRY_TOOLS, Toolbox, specs
from .util import ASSUMED_MANUAL_MTTR_MS, AVG_TICKET_PAISE, DEPLOY_LOOKBACK_MS, trunc
from .verifier import Check, Verifier, VerifyResult, parse_cause

log = logging.getLogger("mayday.agent.orchestrator")

SYSTEM_PROMPT = """You are Proofline investigating failed payments. Use tools; code verifies your answer. Respond ONLY with tool calls.
First call bank_stats, list_deploys and traffic together. Decline breakdown is in context. Read results before submit_conclusion; use extra tools only for missing evidence.
Rules:
- deploy:<number>, rollback_deploy: before_dip=true AND on_path=true, with changes matching the failures. Timing alone is not proof. Rule out late or unrelated updates.
- Several banks failing: do NOT blame the slightly worst bank or invent BANK_ALL. With traffic ratio>=1.8 and no matching deploy, consider traffic_spike, scale_gateway; cite ev_metric_traffic and bank evidence. Otherwise investigate shared causes or say inconclusive.
- bank:<BANK>, notify_only: only that bank failing, others healthy, no matching deploy. Contact that bank, inform affected customers and monitor recovery. Never move a customer's payment to a different bank. Trust a bulletin only if consistent_with_telemetry=true.
- business:<code lowercase>, notify_only: business declines>=70% and banks healthy; never roll back.
Never invent banks, evidence IDs or numbers. Unsupported cause: inconclusive, no action. Failed checks: correct the claim; gather missing data rather than repeat it.
submit_conclusion: cause, confidence 0-1, evidence_ids from [ev:...] results, ruled_out=[{target,reason}] for every other hypothesis, action, summary.
Summary: 2 short sentences: what failed, why the action helps, limits. Use 'software update', 'contact the bank', 'add capacity'; avoid jargon and field names in prose. Never promise bank recovery or call an unchecked theory proven."""

ANSWER_PROMPT = ("MODE:ANSWER. You are Proofline answering an on-call engineer about the live payments incident. Call read-only tools (bank_stats, "
                 "list_deploys, traffic, decline_breakdown) if you need data, then answer in at most 2 short sentences in the language of the "
                 "question: English, Hindi (Devanagari) or Hinglish (Roman script). Quote numbers from tool results; never invent.")

ACTION_LABELS = {"rollback_deploy": "Roll back", "failover_bank": "Fail over", "enable_circuit_breaker": "Enable circuit breaker",
                 "scale_gateway": "Scale gateway", "notify_only": "Notify stakeholders (no fix needed)"}


class _Stop(Exception):
    pass


class _Inconclusive(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _ModelFail(Exception):
    pass


class _Streamer:
    """Turns streamed model text/reasoning into short agent.thought events (<=200 chars each, capped)."""

    def __init__(self, orch, iid: str, via: str, max_n: int = 4, step: int = 160):
        self.o, self.iid, self.via, self.max_n, self.step = orch, iid, via, max_n, step
        self.n = 0
        self.buf = {"text": "", "think": ""}
        self.emitted_text = False

    def _push(self, kind: str, chunk: str) -> None:
        self.buf[kind] += chunk
        b = self.buf[kind]
        if len(b) >= self.step and re.search(r"[.!?]\s*$", b):
            self._flush(kind)

    def feed_text(self, c: str) -> None:
        self._push("text", c)

    def feed_think(self, c: str) -> None:
        self._push("think", c)

    def _flush(self, kind: str) -> None:
        b = re.sub(r"[\s*#`]+", " ", self.buf[kind]).strip()
        self.buf[kind] = ""
        if not b or self.n >= self.max_n:
            return
        self.n += 1
        if kind == "text":
            self.emitted_text = True
        self.o._thought(b, self.via)

    def flush(self) -> None:
        for k in ("think", "text"):
            self._flush(k)


def compute_savings_internal(incident: dict, actual_recovery_ts: int, manual_mttr_ms: int) -> dict:
    rate = (incident.get("blast") or {}).get("paise_per_min_at_risk", 0)
    actual_ms = max(0, actual_recovery_ts - int(incident["start_ts"]))
    actual = int(rate * actual_ms / 60000)
    cf = int(rate * manual_mttr_ms / 60000)
    return {"actual_loss_paise": actual, "counterfactual_loss_paise": cf, "saved_paise": max(0, cf - actual),
            "assumed_manual_mttr_ms": manual_mttr_ms}


def cause_label(cause: str, deploy: Optional[dict] = None) -> str:
    kind, ref = parse_cause(cause)
    if kind == "deploy":
        return f"Bad deploy #{ref}" + (f" on {deploy['service']}" if deploy else "")
    if kind == "bank":
        return f"{ref} outage (bank-side)"
    if kind == "business":
        return "Business declines: " + ref.replace("_", " ")
    if kind == "traffic_spike":
        return "Traffic spike"
    return "Inconclusive"


class Orchestrator:
    def __init__(self, world, bus, gateway: Gateway, *, clock=time.monotonic, approval_ttl_s: float = 300.0,
                 total_budget_s: float = 90.0, model_timeout_s: float = 30.0, max_tool_calls: int = 10,
                 max_verify: int = 3, manual_mttr_ms: int = ASSUMED_MANUAL_MTTR_MS,
                 recovery_timeout_s: float = 30.0, poll_s: float = 0.25):
        from mayday.agent.util import NormWorld
        world = world if isinstance(world, NormWorld) else NormWorld(world)
        self.world, self.bus, self.gateway = world, bus, gateway
        self.tel = world.telemetry
        self.clock = clock
        self.ttl, self.total_budget, self.model_timeout = approval_ttl_s, total_budget_s, model_timeout_s
        self.max_tools, self.max_verify = max_tool_calls, max_verify
        self.mttr_ms, self.recovery_timeout, self.poll_s = manual_mttr_ms, recovery_timeout_s, poll_s
        self.store = EvidenceStore()
        self.verifier = Verifier(self.tel, self.store)
        self._lock = threading.RLock()
        self._state = "idle"
        self._hyps: dict = {}
        self._rca: Optional[dict] = None
        self._incident: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._decision = threading.Event()
        self._decision_val: Optional[str] = None
        self._pending: Optional[dict] = None
        self._last: Optional[dict] = None
        self._timeline: list = []
        self._savings: Optional[dict] = None
        self._executed: Optional[dict] = None
        self._deadline = 0.0
        self.tools = Toolbox(world, self.store, lambda: self._incident, gateway)
        self._steps: list = []
        self._partial = None
        self._ctx: dict = {}
        self.heartbeat_s = float(os.environ.get("MAYDAY_HEARTBEAT_S", "15"))
        self.think_conclude = os.environ.get("MAYDAY_THINK", "0") == "1"

    # ------------------------------------------------------------------ public API
    def set_gateway(self, gateway: Gateway) -> dict:
        """Swap the model at runtime; takes effect from the next model step (an in-flight call finishes on the old one)."""
        self.gateway = gateway
        self.tools.gateway = gateway
        return self.model_status()

    def set_model(self, name: str) -> dict:
        """Pick another installed Ollama model at runtime. Returns a ModelStatus dict."""
        if not hasattr(self.gateway, "set_model"):
            raise ValueError("this gateway cannot switch models")
        self.gateway.set_model(name)
        return self.model_status()

    def model_status(self) -> dict:
        try:
            return dict(self.gateway.status())
        except Exception:
            return {"backend": "ollama", "name": "Gemma unavailable", "healthy": False, "warm": False}

    def model_stats(self) -> list:
        """Per-step model telemetry of the current/last incident: via, ms, prompt/eval tokens."""
        return list(self._steps)

    def get_evidence(self, id: str) -> Optional[dict]:
        return self.store.get(id)

    get = get_evidence

    def on_incident(self, incident: dict) -> None:
        try:
            with self._lock:
                if self._thread and self._thread.is_alive():
                    if self._incident and self._incident.get("id") == incident.get("id"):
                        return
                    self._abort_current()
                self._cancel = threading.Event()
                self._decision = threading.Event()
                self._decision_val, self._pending = None, None
                self._hyps, self._rca, self._timeline, self._savings, self._executed = {}, None, [], None, None
                self._steps, self._partial = [], None
                self._incident = dict(incident)
                t = threading.Thread(target=self._run, args=(self._incident, self._cancel), daemon=True, name="mayday-agent")
                self._thread = t
            t.start()
        except Exception as e:  # never raise into caller
            self._error("agent_start_failed", str(e))

    def join(self, timeout: Optional[float] = None) -> bool:
        t = self._thread
        if t:
            t.join(timeout)
            return not t.is_alive()
        return True

    def _abort_current(self) -> None:
        self._cancel.set()
        self._decision.set()
        try:
            self.gateway.abort()
        except Exception:
            pass
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(2.0)

    def reset(self) -> None:
        with self._lock:
            self._abort_current()
            self._state, self._hyps, self._rca, self._incident = "idle", {}, None, None
            self._pending, self._last = None, None
            self.store.clear()

    def approve(self, incident_id: str, action_id: str, token: str) -> dict:
        with self._lock:
            p = self._pending
            if not p or p["incident_id"] != incident_id:
                return {"ok": False, "error": "no_pending_action"}
            if p["action_id"] != action_id:
                return {"ok": False, "error": "action_mismatch"}
            if p["action"]["kind"] == "failover_bank":
                return {"ok": False, "error": "Payments cannot be transferred to another bank. Contact the affected bank and monitor recovery."}
            if p["used"]:
                return {"ok": False, "error": "token_already_used"}
            if self.clock() > p["expires"]:
                return {"ok": False, "error": "token_expired"}
            if not hmac.compare_digest(str(token or ""), p["token"]):
                return {"ok": False, "error": "bad_token"}
            p["used"] = True
            self._decision_val = "approved"
            self._decision.set()
            return {"ok": True}

    def reject(self, incident_id: str, action_id: str, token: Optional[str] = None) -> dict:
        with self._lock:
            p = self._pending
            if not p or p["incident_id"] != incident_id or p["action_id"] != action_id:
                return {"ok": False, "error": "no_pending_action"}
            if p["used"]:
                return {"ok": False, "error": "token_already_used"}
            p["used"] = True
            self._decision_val = "rejected"
            self._decision.set()
            return {"ok": True}

    def snapshot(self) -> dict:
        with self._lock:
            return {"agent_state": self._state, "hypotheses": list(self._hyps.values()), "rca": self._rca,
                    "model": self.gateway.status(), "verifier_catches": self.verifier.catches,
                    "incident_id": (self._incident or {}).get("id")}

    def last_result(self) -> Optional[dict]:
        return self._last

    def ask(self, text: str, lang: Optional[str] = None) -> str:
        qid = "q_" + uuid.uuid4().hex[:8]
        threading.Thread(target=self._ask_run, args=(qid, text, lang or "auto"), daemon=True, name="mayday-ask").start()
        return qid

    def ask_audio(self, audio: bytes, mime: str = "audio/webm") -> str:
        qid = "q_" + uuid.uuid4().hex[:8]
        if not self.gateway.supports_audio():
            self._error("audio_unsupported", "Native audio input is not available from this Ollama/Gemma server (no audio capability). Use browser speech-to-text and POST /api/ask.", True)
            return qid

        def go():
            try:
                r = self.gateway.transcribe(audio, mime)
                self._ask_run(qid, r.get("transcript", ""), r.get("lang") or "auto")
            except Exception as e:
                self._error("audio_failed", f"{e}. Use browser speech-to-text and POST /api/ask.", True)
        threading.Thread(target=go, daemon=True).start()
        return qid

    # ------------------------------------------------------------------ follow-up drafts (Gemma-written, simulated)
    FOLLOWUP_KINDS = {"permanent_fix": "Engineering", "escalate_bank": "BANK", "merchant_notice": "Merchants", "followup_tasks": "Team"}

    def followup(self, incident_id: str, kind: str) -> dict:
        """Draft follow-up work after an applied fix. Runs in a worker thread; emits followup.ready. Nothing is sent."""
        if kind not in self.FOLLOWUP_KINDS:
            return {"ok": False, "error": f"unknown follow-up kind {kind!r}"}
        with self._lock:
            inc, ex, rca = self._incident, self._executed, self._rca
        if not inc or inc.get("id") != incident_id or not rca:
            return {"ok": False, "error": "unknown incident"}
        if not ex:
            return {"ok": False, "error": "no applied fix yet"}
        threading.Thread(target=self._followup_run, args=(dict(inc), dict(rca), dict(ex), kind), daemon=True, name="mayday-followup").start()
        return {"ok": True}

    def _followup_facts(self, inc: dict, rca: dict, ex: dict) -> dict:
        from .postmortem import _clock
        act = rca.get("proposed_action") or {}
        bl = inc.get("blast") or {}
        bd = inc.get("decline_breakdown") or {}
        kind_, ref = parse_cause(rca.get("cause", ""))
        bank = ref if kind_ == "bank" else (act.get("params") or {}).get("bank") or (self._ctx.get("worst_bank") or {}).get("bank") or "the bank"
        return {"cause": rca.get("cause_label"), "cause_id": rca.get("cause"), "kind": kind_, "bank": bank,
                "summary": re.sub(r"\s*\(via [^)]*\)\s*$", "", rca.get("summary", "")), "scope": bl.get("scope"), "failing_pct": bl.get("failing_pct"),
                "failed_txns": bl.get("failed_txns"), "codes": ", ".join(f"{c.get('code')} {c.get('pct')}%" for c in (bd.get("top_codes") or [])[:3]),
                "action": act.get("label"), "params": act.get("params") or {}, "warnings": act.get("warnings") or [],
                "recovered": ex.get("recovered"), "predicted_s": (ex.get("predicted_recovery_ms") or 0) // 1000,
                "actual_s": (ex.get("actual_recovery_ms") or 0) // 1000, "ruled_out": [r.get("target") for r in rca.get("ruled_out", [])],
                "evidence": [e["id"] for e in rca.get("evidence", [])], "start": _clock(int(inc.get("start_ts", 0))) + " UTC",
                "timeline": [f"{_clock(t)} {x}" for t, x in sorted(self._timeline)[:10]], "saved_paise": (self._savings or {}).get("saved_paise")}

    _FU_TASK = {
        "permanent_fix": "Write a permanent-fix change request for engineers. If the cause is a bad deploy: a revert PR description (title, why, risk), a config pin line, and the regression test to add. If the cause is a bank outage: a routing change request that keeps traffic on the healthy bank until the failing bank is back, plus the exact check to switch back.",
        "escalate_bank": "Write an incident ticket to the bank operations team: timeline, error codes, affected services, our evidence ids, what we changed on our side, and ask for an ETA and root cause.",
        "merchant_notice": "Write a short plain-English notice to merchants (what happened, impact, what we did, what to expect), then the same notice in Hindi (Devanagari). Calm tone, no jargon.",
        "followup_tasks": "Write 3-5 action items as a markdown list, each with owner and priority (P1/P2), e.g. timeout-config alert, canary rollout, bank status monitor. Make them specific to this incident."}
    _FU_TITLE = {"permanent_fix": "Permanent fix: {cause}", "escalate_bank": "Escalation ticket to {bank} operations",
                 "merchant_notice": "Merchant notice / व्यापारी सूचना", "followup_tasks": "Follow-up tasks: {cause}"}

    def _followup_run(self, inc: dict, rca: dict, ex: dict, kind: str) -> None:
        iid = inc["id"]
        f = self._followup_facts(inc, rca, ex)
        title = self._FU_TITLE[kind].format(cause=f["cause"], bank=f["bank"])
        audience = f["bank"] if kind == "escalate_bank" else self.FOLLOWUP_KINDS[kind]
        body = None
        try:
            facts = json.dumps({k: v for k, v in f.items() if v not in (None, "", [], {})}, default=str, separators=(",", ":"))
            r = self.gateway.chat([{"role": "system", "content": "You are Proofline, drafting incident follow-up documents for a UPI payments team. "
                                    "Use ONLY the facts given; never invent numbers, names or ids. Markdown, at most 200 words, no preamble. "
                                    "This is a draft; nothing is sent."},
                                   {"role": "user", "content": f"TASK: {self._FU_TASK[kind]}\nFACTS: {facts}"}],
                                  None, timeout=self.model_timeout, num_predict=520, think=False)
            body = (r.text or "").strip() or None
        except Exception as e:
            log.warning("followup via model failed: %s", e)
        if not body:
            title += " (template)"
            body = self._followup_template(kind, f)
        words = body.split()
        if len(words) > 220:
            body = " ".join(words[:220]) + " …"
        self._emit("followup.ready", {"incident_id": iid, "kind": kind, "title": trunc(title, 120), "body": body,
                                      "audience": audience, "simulated": True})

    @staticmethod
    def _followup_template(kind: str, f: dict) -> str:
        bank, tl = f["bank"], "\n".join(f"- {x}" for x in f["timeline"][:6])
        if kind == "permanent_fix":
            if f["kind"] == "deploy":
                return (f"**Revert PR:** revert {f['cause']}. Reason: {f['summary']}\n\n**Config pin:** pin the previous known-good config until the fix is re-released.\n\n"
                        f"**Test to add:** a timeout/config validation test that fails when per-bank settings are tightened without a canary.")
            return (f"**Routing change request:** keep traffic on the healthy bank until {bank} recovers ({f['action']}).\n\n"
                    f"**Switch-back check:** {bank} technical success back at baseline for 15 minutes and its bulletin cleared.")
        if kind == "escalate_bank":
            return (f"**Incident ticket to {bank} operations**\n\nImpact: {f['failing_pct']}% of payments failing; codes: {f['codes']}.\n\nTimeline:\n{tl}\n\n"
                    f"Evidence ids: {', '.join(f['evidence'])}\n\nWe changed on our side: {f['action']}.\n\nPlease share root cause and an ETA.")
        if kind == "merchant_notice":
            return (f"We saw payment failures starting around {f['start']} ({f['cause']}). We applied a fix and are monitoring. Some payments may have failed; please retry.\n\n"
                    f"हमने {f['start']} के आसपास भुगतान में विफलताएँ देखीं। हमने सुधार लागू कर दिया है और निगरानी कर रहे हैं। कृपया भुगतान दोबारा करें।")
        return ("- [P1] Engineering: add an alert on per-bank timeout config changes\n- [P1] Engineering: canary rollout for payment-path deploys\n"
                f"- [P2] SRE: bank status monitor for {bank}\n- [P2] Support: send merchant notice and track complaints")

    # ------------------------------------------------------------------ plumbing
    def _now(self) -> int:
        try:
            return int(self.tel.now())
        except Exception:
            return 0

    def _emit(self, type: str, data: dict) -> None:
        try:
            self.bus.emit(type, data, ts=self._now())
        except Exception:
            pass

    def _thought(self, text: str, via: Optional[str] = None, iid: Optional[str] = None) -> None:
        suffix = f" — via {via}" if via else ""
        self._emit("agent.thought", {"incident_id": iid or (self._incident or {}).get("id", ""),
                                     "text": trunc(" ".join(str(text).split()), 200 - len(suffix)) + suffix})

    def _live_via(self) -> str:
        g = getattr(self.gateway, "primary", None) or self.gateway
        return getattr(g, "name", "model")

    def _error(self, code: str, message: str, recoverable: bool = True) -> None:
        self._emit("error", {"code": code, "message": trunc(message, 300), "recoverable": recoverable})

    def _set_state(self, state: str, note: Optional[str] = None) -> None:
        self._state = state
        d = {"incident_id": (self._incident or {}).get("id", ""), "state": state}
        if note:
            d["note"] = trunc(note, 160)
        self._emit("agent.state", d)

    def _tl(self, ts: int, text: str) -> None:
        self._timeline.append((ts, text))

    def _check(self, cancel) -> None:
        if cancel.is_set():
            raise _Stop()

    def _remaining(self) -> float:
        return self._deadline - self.clock()

    # ------------------------------------------------------------------ run
    def _run(self, incident: dict, cancel: threading.Event) -> None:
        iid = incident["id"]
        try:
            self._deadline = self.clock() + self.total_budget
            self._triage(incident)
            self._check(cancel)
            try:
                concl, rca_parts = self._reason(incident, cancel)
            except _Inconclusive as e:
                self._finish_inconclusive(incident, e.reason)
                return
            self._check(cancel)
            self._after_verified(incident, concl, rca_parts, cancel)
        except _Stop:
            return
        except _ModelFail as e:
            self._error("model_error", str(e), True)
            self._set_state("error", "model unavailable; agent can be retried")
            self._last = {"incident_id": iid, "agent_cause": None, "inconclusive": True, "verified": False,
                          "action_proposed": False, "action_kind": None, "error": str(e)}
        except Exception as e:
            self._error("agent_crash", f"{type(e).__name__}: {e}", True)
            self._set_state("error", "agent error; recoverable")
            self._last = {"incident_id": iid, "agent_cause": None, "inconclusive": True, "verified": False,
                          "action_proposed": False, "action_kind": None, "error": str(e)}

    # ---- TRIAGE
    def _triage(self, incident: dict) -> None:
        self._set_state("triage", "decline breakdown and blast radius")
        s, now = int(incident["start_ts"]), self._now()
        self._tl(s, "Incident began (back-dated start)")
        self._tl(int(incident.get("detected_ts", now)), "Detected by ProofLine")
        bd = incident.get("decline_breakdown") or self.tel.decline_breakdown(s, max(now, s + 1000), None)
        incident["decline_breakdown"] = {k: bd.get(k) for k in ("technical_pct", "business_pct", "top_codes")}
        self.store.register("ev_breakdown_triage", "breakdown", "Decline breakdown (triage)", incident["decline_breakdown"], ts=now)
        cur = self.tel.bank_stats(s, max(now, s + 1000))
        base = self.tel.bank_stats(s - 300_000, s - 15_000)
        d = {b: cur[b].get("technical_success_rate", 0) - base.get(b, {}).get("technical_success_rate", 0) for b in cur}
        worst = min(d, key=d.get) if d else "BANK_X"
        if not incident.get("blast"):
            tps = sum(v.get("tps", 0) for v in cur.values())
            frac = max(0.0, incident.get("baseline_success", 1) - incident.get("current_success", 1))
            per_min = tps * 60 * frac
            incident["blast"] = {"scope": f"{worst} via payment-orchestrator" if d.get(worst, 0) < -0.05 and all(v >= -0.03 for b, v in d.items() if b != worst) else "payments (all banks)",
                                 "failing_pct": round(frac * 100, 1),
                                 "failed_txns": int(per_min * max(0.1, (now - s) / 60000)),
                                 "paise_per_min_at_risk": int(per_min * AVG_TICKET_PAISE)}
            self._emit("incident.updated", {"incident": incident})
        self._incident = incident
        deploys = self.tel.deploys(s - DEPLOY_LOOKBACK_MS, now + 1)
        hyps = [{"id": f"h_deploy_{x['number']}", "kind": "deploy", "target": f"deploy:{x['number']}", "status": "open",
                 "reason": trunc(f"#{x['number']} on {x['service']}: timing and scope unchecked", 120)}
                for x in sorted(deploys, key=lambda x: x["ts"])]
        hyps += [{"id": f"h_bank_{worst}", "kind": "bank_outage", "target": f"bank:{worst}", "status": "open", "reason": "Bank-side failure not yet checked"},
                 {"id": "h_traffic", "kind": "traffic_spike", "target": "traffic_spike", "status": "open", "reason": "Traffic volume not yet checked"},
                 {"id": "h_business", "kind": "business_decline", "target": "business_decline", "status": "open", "reason": "User-side declines not yet checked"}]
        with self._lock:
            self._hyps = {h["id"]: h for h in hyps}
        for h in hyps:
            self._emit("hypothesis.update", {"incident_id": incident["id"], "hypothesis": dict(h)})
        self._thought(f"Triage: {bd.get('technical_pct', 0):.0f}% technical / {bd.get('business_pct', 0):.0f}% business declines; "
                      f"scope {incident['blast']['scope']}; {len(deploys)} recent deploy(s).", "code", incident["id"])
        self._ctx = {"incident": {k: incident.get(k) for k in ("id", "start_ts", "detected_ts", "baseline_success", "current_success", "blast")},
                     "triage": incident["decline_breakdown"], "hypotheses": hyps,
                     "bank_changes_pp": {b: round(v * 100, 1) for b, v in d.items()}}
        if d.get(worst, 0) < -0.05 and all(v >= -0.03 for b, v in d.items() if b != worst):
            self._ctx["worst_bank"] = {"bank": worst, "d_pp": round(d[worst] * 100, 1)}

    # ---- INVESTIGATE / CONCLUDE / VERIFY loop
    def _model(self, messages: list, tools: list, cancel, think: bool = False, step: int = 1):
        iid = (self._incident or {}).get("id", "")
        for attempt in (1, 2):
            self._check(cancel)
            rem = self._remaining()
            if rem <= 0:
                raise _Inconclusive(f"{self.total_budget:.0f}s time budget exhausted")
            live = getattr(self.gateway, "backend", "") == "ollama"
            st = _Streamer(self, iid, self._live_via()) if live else None
            hb_stop = threading.Event()
            if live and self.heartbeat_s > 0:
                t0 = time.monotonic()

                def beat():
                    n = 0
                    while not hb_stop.wait(self.heartbeat_s) and n < 8:
                        n += 1
                        self._thought(f"Still working on step {step} ({time.monotonic() - t0:.0f}s elapsed)", self._live_via(), iid)
                threading.Thread(target=beat, daemon=True, name="mayday-heartbeat").start()
            t_call = time.monotonic()
            try:
                reply = self.gateway.chat(messages, tools, think=think, timeout=min(self.model_timeout, rem), require_tool=True,
                                          on_text=st.feed_text if st else None, on_think=None, num_predict=256 if step == 1 else 450,
                                          should_stop=cancel.is_set)
            except ModelAborted:
                raise _Stop()
            except ModelTimeout as e:
                raise _ModelFail(f"model timed out: {e}")
            except ModelError as e:
                if attempt == 2:
                    raise _ModelFail(str(e))
                continue
            finally:
                hb_stop.set()
                if st:
                    st.flush()
            self._note_reply(reply, step, time.monotonic() - t_call, st)
            return reply
        raise _ModelFail("model failed")

    def _note_reply(self, reply, step: int, secs: float, st) -> None:
        via = reply.via or getattr(self.gateway, "name", "model")
        rec = {"step": step, "via": via, "ms": int(secs * 1000),
               "calls": [c.name for c in reply.tool_calls], **{k: v for k, v in (reply.stats or {}).items() if k in ("prompt_tokens", "eval_tokens", "think", "reasked")}}
        self._steps.append(rec)
        log.info("model step %s", rec)
        if reply.text and not (st and st.emitted_text):
            self._thought(reply.text, via)
        if len(reply.tool_calls) > 1:
            self._thought("Requested in parallel: " + ", ".join(c.name for c in reply.tool_calls), via)

    def _via_tag(self, concl_via: str) -> str:
        return f" (via {concl_via or getattr(self.gateway, 'name', 'model')})"

    def _reason(self, incident: dict, cancel) -> tuple:
        iid = incident["id"]
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "CONTEXT:\n" + json.dumps(self._ctx, default=str)}]
        tools = specs(TELEMETRY_TOOLS + CONTROL_TOOLS)
        used = verify_attempts = nudges = over_budget_turns = 0
        self._set_state("investigating")
        n_call = turn = 0
        while True:
            turn += 1
            think = self.think_conclude and (turn >= 2 or verify_attempts > 0)
            reply = self._model(messages, tools, cancel, think=think, step=turn)
            self._check(cancel)
            calls = reply.tool_calls
            messages.append({"role": "assistant", "content": reply.text or "",
                             "tool_calls": [{"id": c.id, "function": {"name": c.name, "arguments": c.args}} for c in calls]})
            if not calls:
                nudges += 1
                if nudges > 3:
                    raise _Inconclusive("model did not use tools or submit a conclusion")
                messages.append({"role": "user", "content": "Use the tools, then call submit_conclusion."})
                continue
            concl = None
            batch = [c for c in calls if c.name in TELEMETRY_TOOLS and c.name != "preview_fix"]
            for c in calls:
                if concl is not None:
                    messages.append(self._toolmsg(c, {"ok": False, "summary": "ignored after conclusion", "data": {}, "evidence_ids": []}))
                    continue
                if c.name == "update_hypothesis":
                    messages.append(self._toolmsg(c, self._update_hyp(iid, c.args)))
                elif c.name == "submit_conclusion":
                    if batch:   # concluded before seeing the results of tools requested in the same turn
                        messages.append(self._toolmsg(c, {"ok": False, "summary": "read the tool results first, then submit_conclusion", "data": {}, "evidence_ids": []}))
                        continue
                    concl = self._norm_conclusion(c.args, incident)
                    concl["_via"] = reply.via
                    self._thought(f"Concluding {concl['cause']} (confidence {concl['confidence']:.0%}): {concl.get('summary') or ''}", reply.via, iid)
                    messages.append(self._toolmsg(c, {"ok": True, "summary": "conclusion received; verifying", "data": {}, "evidence_ids": []}))
                elif c.name in TELEMETRY_TOOLS:
                    if used >= self.max_tools:
                        messages.append(self._toolmsg(c, {"ok": False, "summary": "tool budget exhausted", "data": {}, "evidence_ids": []}))
                        continue
                    used += 1
                    n_call += 1
                    cid = f"c{n_call}"
                    self._emit("agent.tool_call", {"incident_id": iid, "call_id": cid, "tool": c.name, "args": c.args})
                    r = self.tools.call(c.name, c.args)
                    self._emit("agent.tool_result", {"incident_id": iid, "call_id": cid, "tool": c.name, "summary": trunc(r.summary, 160),
                                                    "ok": r.ok, "evidence_ids": r.evidence_ids, "ms": r.ms})
                    messages.append(self._toolmsg(c, {"ok": r.ok, "summary": r.summary, "data": r.data, "evidence_ids": r.evidence_ids}))
                else:
                    messages.append(self._toolmsg(c, {"ok": False, "summary": f"unknown tool {c.name}", "data": {}, "evidence_ids": []}))
            if concl is None:
                if used >= self.max_tools:
                    over_budget_turns += 1
                    if over_budget_turns > 2:
                        raise _Inconclusive("tool-call budget exhausted without a conclusion")
                    messages.append({"role": "user", "content": "TOOL BUDGET EXHAUSTED. Call submit_conclusion now."})
                continue
            # ---- conclude + verify
            self._set_state("concluding")
            if concl["cause"] == "inconclusive":
                raise _Inconclusive(concl.get("summary") or "model reported inconclusive")
            self._mark_claimed(iid, concl)
            verify_attempts += 1
            res, preview, action = self._verify(incident, concl, verify_attempts)
            if res.passed:
                return concl, {"preview": preview, "action": action}
            if res.failed and all(c.name in self.SAFETY_CHECKS for c in res.failed):
                alt = self._safe_alternative(incident, concl, res, verify_attempts)
                if alt:
                    self._thought(f"Twin says {(concl.get('proposed_action') or {}).get('kind')} is unsafe; choosing a safer option in code: "
                                  f"{alt[0]['proposed_action']['kind']}", "code", iid)
                    return alt
            if verify_attempts >= self.max_verify:
                self._partial = concl
                raise _Inconclusive("verifier rejected all conclusions: " + (res.rejected_reason or ""))
            self._reopen_claimed(iid, concl)
            self._set_state("investigating", "verifier rejected; self-correcting")
            fb = {"checks": [c.to_feedback() for c in res.checks], "action_kind": (concl.get("proposed_action") or {}).get("kind"),
                  "instruction": "Fix the failed checks. If several banks are failing, do not blame one bank or invent BANK_ALL. Recheck traffic and shared services using tools if needed. Cite only existing evidence. If no cause can be supported, submit inconclusive rather than repeat a rejected claim."}
            messages.append({"role": "user", "content": "VERIFIER_REJECTED\n" + json.dumps(fb)})
            self._partial = concl

    def _mark_claimed(self, iid: str, concl: dict) -> None:
        with self._lock:
            h = next((h for h in self._hyps.values() if h["target"] == concl["cause"] and h["status"] == "open"), None)
            if not h:
                return
            h["status"], h["reason"] = "supported", trunc(concl.get("summary") or "claimed by the model; verifying", 120)
            snap = dict(h)
        self._emit("hypothesis.update", {"incident_id": iid, "hypothesis": snap})

    @staticmethod
    def _toolmsg(c, payload: dict) -> dict:
        return {"role": "tool", "tool_call_id": c.id, "tool_name": c.name, "content": json.dumps(payload, default=str)}

    def _update_hyp(self, iid: str, args: dict) -> dict:
        hid, status = args.get("id"), args.get("status")
        with self._lock:
            h = self._hyps.get(hid)
            if not h:
                return {"ok": False, "summary": "unknown hypothesis id", "data": {"valid_ids": list(self._hyps)}, "evidence_ids": []}
            if status == "confirmed":
                status = "supported"
            if status not in ("open", "supported", "ruled_out"):
                return {"ok": False, "summary": "bad status", "data": {}, "evidence_ids": []}
            h["status"], h["reason"] = status, trunc(args.get("reason", ""), 120)
            snap = dict(h)
        self._emit("hypothesis.update", {"incident_id": iid, "hypothesis": snap})
        if status == "ruled_out":
            self._tl(self._now(), f"Ruled out {h['target']}: {h['reason']}")
        return {"ok": True, "summary": f"{hid} -> {status}", "data": {}, "evidence_ids": []}

    def _reopen_claimed(self, iid: str, concl: dict) -> None:
        with self._lock:
            for h in self._hyps.values():
                if h["status"] == "supported" and h["target"] == concl["cause"]:
                    h["status"], h["reason"] = "open", "Verifier rejected this claim; re-examining"
                    snap = dict(h)
                    break
            else:
                return
        self._emit("hypothesis.update", {"incident_id": iid, "hypothesis": snap})

    # ---- conclusion normalisation: tolerate the sloppy-but-recoverable shapes small models produce
    @staticmethod
    def _norm_cause(c) -> str:
        c = str(c or "").strip()
        low = c.lower()
        m = re.match(r"^(?:deploy)?[\s:#_-]*#?(\d{2,})$", low) or re.match(r"^deploy[\s:#_-]*(\d+)", low)
        if m:
            return f"deploy:{m.group(1)}"
        if low.startswith("bank") or c.upper().startswith("BANK_"):
            m = re.search(r"BANK_[A-Z0-9]+", c.upper().replace(" ", "_"))
            if m:
                return f"bank:{m.group(0)}"
            m = re.match(r"^bank[\s:_-]+([a-z0-9]+)$", low)
            if m:
                return f"bank:BANK_{m.group(1).upper()}"
        if low.startswith("business"):
            code = re.sub(r"^business[\s:_-]*(bd_)?", "", low).strip().replace(" ", "_")
            return "business:" + (code or "unknown")
        if low.startswith("traffic"):
            return "traffic_spike"
        if low.startswith("inconclusive"):
            return "inconclusive"
        return c

    def _norm_target(self, t) -> str:
        t = str(t or "").strip()
        with self._lock:
            targets = {h["target"] for h in self._hyps.values()}
            by_id = {h["id"]: h["target"] for h in self._hyps.values()}
        if t in targets:
            return t
        if t in by_id:
            return by_id[t]
        if re.search(r"business", t, re.I):
            return "business_decline"
        return self._norm_cause(t)

    def _norm_conclusion(self, a: dict, incident: Optional[dict] = None) -> dict:
        a = a or {}
        ev = a.get("evidence_ids") or []
        if isinstance(ev, str):
            ev = re.findall(r"ev_[A-Za-z0-9_]+", ev) or [ev]
        ro = []
        for r in (a.get("ruled_out") or []):
            if isinstance(r, dict) and ("target" in r or "reason" in r):
                ro.append({"target": self._norm_target(r.get("target", "")), "reason": trunc(r.get("reason", ""), 120)})
            elif isinstance(r, dict) and r:
                k, v = next(iter(r.items()))
                ro.append({"target": self._norm_target(k), "reason": trunc(v, 120)})
            elif isinstance(r, str) and r.strip():
                tgt, _, why = r.strip().partition(" ")
                ro.append({"target": self._norm_target(tgt.rstrip(":,;-")), "reason": trunc(why.lstrip("-:; ") or "ruled out", 120)})
        cause = self._norm_cause(a.get("cause", "inconclusive"))
        act = a.get("proposed_action") or a.get("action")
        if isinstance(act, str) and act.strip():
            act = {"kind": act.strip()}
        if isinstance(act, dict) and act.get("kind"):
            reroute = self._KIND_ALIASES.get(str(act["kind"]).strip().lower(), str(act["kind"]).strip().lower()) == "failover_bank"
            act = self._fill_action(act, cause, incident)
            if reroute:
                bank = parse_cause(cause)[1] if cause.startswith("bank:") else "the affected bank"
                a = dict(a, summary=f"Payments using {bank} are failing. No safe automatic fix can repair the bank; contact its support team, inform affected customers and monitor recovery.")
        else:
            act = None
        try:
            conf = a.get("confidence", 0.5)
            conf = float(str(conf).rstrip("%")) / (100.0 if "%" in str(conf) else 1.0)
            conf = max(0.0, min(1.0, conf))
        except Exception:
            conf = 0.5
        return {"cause": cause, "confidence": conf, "evidence_ids": [str(e) for e in ev if e],
                "ruled_out": ro, "proposed_action": act, "summary": trunc(a.get("summary", ""), 600)}

    _KIND_ALIASES = {"rollback": "rollback_deploy", "failover": "failover_bank", "notify": "notify_only",
                     "circuit_breaker": "enable_circuit_breaker", "scale": "scale_gateway"}

    def _fill_action(self, act: dict, cause: str, incident: Optional[dict]) -> dict:
        kind = str(act.get("kind", "")).strip().lower().replace(" ", "_")
        kind = self._KIND_ALIASES.get(kind, kind)
        given = {k: str(v) for k, v in (act.get("params") or {}).items()} if isinstance(act.get("params"), dict) else {}
        ck, ref = parse_cause(cause)
        if kind == "failover_bank":
            return {"kind": "notify_only", "params": {}, "rationale": "No safe automatic fix can move a customer's payment to another bank. We recommend contacting the affected bank, informing customers and monitoring recovery."}
        params = {}
        try:
            inc = incident or self._incident or {"start_ts": self._now()}
            if kind == "rollback_deploy" and ck == "deploy":
                d = self.verifier._find_deploy(ref, inc)
                params = {"deploy_id": (d or {}).get("id", f"dep_{ref}"), "number": str(ref), "service": (d or {}).get("service", "")}
            elif kind == "failover_bank":
                bank = ref if ck == "bank" else None
                if bank is None and ck == "deploy":
                    bank = self._bank_in(self.verifier._find_deploy(ref, inc))
                bank = bank or (self._ctx.get("worst_bank") or {}).get("bank")
                if bank:
                    s0 = int(inc.get("start_ts", self._now()))
                    cur = self.tel.bank_stats(s0, max(self._now(), s0 + 1000))
                    others = sorted((b for b in cur if b != bank), key=lambda b: -cur[b].get("technical_success_rate", 0))
                    params = {"bank": bank, "from_bank": bank, "to_bank": others[0] if others else ""}
        except Exception:
            params = {}
        params.update(given)
        return {"kind": kind, "params": params, "rationale": trunc(act.get("rationale") or self._plain_rationale(kind, params, cause), 200)}

    @staticmethod
    def _plain_rationale(kind: str, p: dict, cause: str) -> str:
        if kind == "rollback_deploy":
            return f"Undo deploy #{p.get('number', '')} on {p.get('service', 'the service')}, which started the failures."
        if kind == "failover_bank":
            return (f"Route {p.get('bank', 'the failing bank')} payments through {p.get('to_bank') or 'a healthy bank'} so customers can pay "
                    f"while {p.get('bank', 'it')} is down.")
        if kind == "enable_circuit_breaker":
            return "Stop retrying the failing path so errors do not pile up while the cause is fixed."
        if kind == "scale_gateway":
            return "Add gateway capacity to absorb the extra traffic."
        if kind == "notify_only":
            return "Nothing is broken on our side; inform stakeholders instead of changing infrastructure."
        return f"Apply {kind}."

    SAFETY_CHECKS = ("twin_predicts_recovery", "failover_target_healthy")

    def _preview(self, iid: str, act: dict, attempt: int) -> dict:
        self._set_state("previewing", "running fix in digital twin")
        try:
            pv = self.world.preview_action(act["kind"], act["params"], 120)
            preview = {"recovers": bool(pv.get("recovers")), "expected_recovery_ms": int(pv.get("expected_recovery_ms", 0)),
                       "predicted": pv.get("predicted", []), "baseline": pv.get("baseline", []),
                       "warnings": [str(w) for w in (pv.get("warnings") or [])]}
            for k in ("target_bank", "target_headroom_pct", "recovery_pct"):
                if pv.get(k) is not None:
                    preview[k] = pv[k]
        except Exception as e:
            self._error("twin_failed", str(e))
            preview = {"recovers": False, "expected_recovery_ms": 0, "predicted": [], "baseline": [], "warnings": ["twin preview failed"]}
        self._emit("twin.preview", {"incident_id": iid, "action_id": f"act_{iid}_{attempt}", "kind": act["kind"],
                                    "recovers": preview["recovers"], "expected_recovery_ms": preview["expected_recovery_ms"],
                                    "predicted": preview["predicted"], "baseline": preview["baseline"]})
        return preview

    def _safety(self, res, act: dict, preview: dict, incident: dict) -> None:
        self.verifier.append_check(res, self.verifier.twin_check(preview))
        if act["kind"] == "failover_bank":
            self.verifier.append_check(res, self.verifier.failover_check(act, preview, incident))

    def _verify(self, incident: dict, concl: dict, attempt: int) -> tuple:
        iid = incident["id"]
        self._set_state("verifying", f"attempt {attempt}/{self.max_verify}")
        res = self.verifier.verify(concl, incident)
        act = concl.get("proposed_action")
        preview = None
        if res.passed and act and act["kind"] != "notify_only":
            preview = self._preview(iid, act, attempt)
            self._safety(res, act, preview, incident)
        self._emit_verify(iid, attempt, res)
        return res, preview, act

    def _emit_verify(self, iid: str, attempt: int, res) -> None:
        d = {"incident_id": iid, "attempt": attempt, "passed": res.passed, "checks": [c.to_contract() for c in res.checks]}
        if not res.passed:
            d["rejected_reason"] = res.rejected_reason
        self._emit("verify.result", d)
        self._tl(self._now(), f"Verify attempt {attempt}: {'passed' if res.passed else 'rejected - ' + (res.rejected_reason or '')[:100]}")

    # ---- safe-alternative selection (code, not model): the cause stays verified, the fix must be safe
    def _alt_ladder(self, ck: str, tried: set) -> list:
        base = {"deploy": ["rollback_deploy"], "bank": [],
                "traffic_spike": ["scale_gateway", "enable_circuit_breaker"]}.get(ck, [])
        return [k for k in base if k not in tried]

    def _safe_alternative(self, incident: dict, concl: dict, res0, attempt: int) -> Optional[tuple]:
        iid = incident["id"]
        ck, ref = parse_cause(concl["cause"])
        first = (concl.get("proposed_action") or {}).get("kind")
        tried = {first} if first else set()
        why0 = "; ".join(c.detail for c in res0.failed)[:160]
        for kind in self._alt_ladder(ck, tried):
            act = self._fill_action({"kind": kind}, concl["cause"], incident)
            if kind == "failover_bank" and not act["params"].get("bank"):
                continue
            attempt += 1
            res = self.verifier.verify(dict(concl, proposed_action=act), incident)
            if not res.passed:
                continue
            pv = self._preview(iid, act, attempt)
            self._safety(res, act, pv, incident)
            self._emit_verify(iid, attempt, res)
            if res.passed:
                c2 = dict(concl, proposed_action=act, summary=trunc(concl["summary"], 400) + " The simulated test rejected the first fix. This alternative passed the checks.")
                return c2, {"preview": pv, "action": act}
        # nothing safe: honest escalation, no automatic change
        bank = (self._ctx.get("worst_bank") or {}).get("bank") or (ref if ck == "bank" else "the affected bank")
        next_step = f"We recommend contacting {bank} and informing affected merchants." if ck == "bank" else "Ask an engineer to investigate and inform the people affected."
        act = {"kind": "notify_only", "params": {},
               "rationale": "No safe automatic fix passed the checks. " + next_step}
        attempt += 1
        res = VerifyResult(checks=[c for c in res0.checks if c.passed])
        res.checks.append(Check("no_safe_automatic_fix", True, "OK", trunc(f"no automatic action passed the twin; escalating to humans ({why0})", 200)))
        self._emit_verify(iid, attempt, res)
        c2 = dict(concl, proposed_action=act, summary=trunc(concl["summary"], 420) + " No safe automatic fix was found. " + next_step)
        c2["_prev_warnings"] = [why0]
        return c2, {"preview": None, "action": act}

    # ---- post-verification
    def _after_verified(self, incident: dict, concl: dict, parts: dict, cancel) -> None:
        iid = incident["id"]
        kind, ref = parse_cause(concl["cause"])
        deploy = None
        if kind == "deploy":
            deploy = self.verifier._find_deploy(ref, incident)
        with self._lock:
            for h in self._hyps.values():
                if h["target"] == concl["cause"] or (kind == "business" and h["kind"] == "business_decline"):
                    h["status"], h["reason"] = "confirmed", trunc(concl["summary"], 120)
                elif any(r["target"] == h["target"] for r in concl["ruled_out"]):
                    h["status"] = "ruled_out"
                    h["reason"] = trunc(next(r["reason"] for r in concl["ruled_out"] if r["target"] == h["target"]), 120)
            snaps = [dict(h) for h in self._hyps.values()]
        for h in snaps:
            self._emit("hypothesis.update", {"incident_id": iid, "hypothesis": h})
        action = None
        pv = parts.get("preview")
        a = concl.get("proposed_action")
        if a:
            action = {"id": f"act_{iid}_{uuid.uuid4().hex[:6]}", "kind": a["kind"], "params": a["params"],
                      "label": self._action_label(a, deploy), "rationale": a.get("rationale") or ""}
            if pv:
                action["preview"] = {"recovers": pv["recovers"], "expected_recovery_ms": pv["expected_recovery_ms"], "series": pv["predicted"]}
            action["warnings"] = self._action_warnings(a, concl["cause"], pv, concl.get("_prev_warnings"))
        rca = {"incident_id": iid, "cause": concl["cause"], "cause_label": cause_label(concl["cause"], deploy),
               "confidence": concl["confidence"],
               "evidence": [{"id": e, "kind": self.store.get(e)["kind"], "title": self.store.get(e)["title"]} for e in concl["evidence_ids"] if self.store.get(e)],
               "ruled_out": concl["ruled_out"], "summary": concl["summary"]}
        if not action:
            rca["summary"] = trunc(rca["summary"] + " No safe automated action was found.", 560)
        tag = self._via_tag(concl.get("_via", ""))
        rca["summary"] = trunc(rca["summary"], 600 - len(tag)) + tag
        if action:
            rca["proposed_action"] = action
        with self._lock:
            self._rca = rca
        blast = incident.get("blast", {})
        if kind in ("deploy", "bank") and blast:
            bank = ref if kind == "bank" else (self._bank_in(deploy))
            if bank:
                blast["scope"] = f"{bank} via payment-orchestrator"
                self._emit("incident.updated", {"incident": incident})
        self._emit("rca.ready", {"rca": rca})
        self._tl(self._now(), f"RCA verified: {rca['cause_label']}")
        self._last = {"incident_id": iid, "agent_cause": concl["cause"], "inconclusive": False, "verified": True,
                      "action_proposed": action is not None, "action_kind": action["kind"] if action else None, "rca": rca,
                      "via": concl.get("_via", ""), "steps": list(self._steps)}
        executed = None
        if action:
            self._set_state("proposing")
            token = secrets.token_urlsafe(16)
            with self._lock:
                self._pending = {"incident_id": iid, "action_id": action["id"], "token": token, "used": False,
                                 "expires": self.clock() + self.ttl, "action": action}
            self._emit("action.proposed", {"incident_id": iid, "action": action, "approval_token": token,
                                           "expires_ts": self._now() + int(self.ttl * 1000)})
            self._set_state("awaiting_approval")
            self._tl(self._now(), f"Proposed: {action['label']}")
            outcome = self._await_decision(cancel)
            if outcome == "approved":
                executed = self._remediate(incident, action, pv, cancel)
            elif outcome == "rejected":
                self._tl(self._now(), "Operator rejected the proposed action")
            else:
                self._tl(self._now(), "Approval expired")
                self._error("approval_expired", "Approval token expired; incident remains open", True)
                self._set_state("idle", "approval expired")
                return
        self._executed = executed
        self._set_state("comms")
        self._emit("comms.drafted", {"incident_id": iid, "drafts": draft_comms(rca, incident, bool(executed and executed.get("recovered")))})
        self._set_state("postmortem")
        md = build_postmortem(rca, incident, self._timeline, self._savings, executed, self.gateway)
        self._emit("postmortem.ready", {"incident_id": iid, "markdown": md})
        self._set_state("idle", "incident handled")

    @staticmethod
    def _bank_in(deploy) -> Optional[str]:
        import re
        m = re.findall(r"BANK_[XYZ]", (deploy or {}).get("diff", "") + (deploy or {}).get("summary", ""))
        return m[0] if m else None

    @staticmethod
    def _action_warnings(a: dict, cause: str, pv: Optional[dict], extra=None) -> list:
        ck, ref = parse_cause(cause)
        p = a.get("params") or {}
        w = list((pv or {}).get("warnings") or [])
        if a["kind"] == "failover_bank":
            bank = p.get("bank") or p.get("from_bank") or "the bank"
            to = p.get("to_bank") or (pv or {}).get("target_bank") or "the target bank"
            if ck == "bank":
                w.append(f"This works around the problem; {bank} itself is still down and needs the bank to fix it.")
            head = (pv or {}).get("target_headroom_pct")
            w.append(f"Failover assumes a routing agreement and spare capacity at {to}" + (f" (twin estimates ~{head:.0f}% headroom)." if head is not None else "."))
        if a["kind"] == "notify_only" and extra:
            w += [f"No safe automatic fix: {x}" for x in extra] + ["Contact the bank and inform merchants."]
        out = []
        for x in w:
            if x and x not in out:
                out.append(trunc(x, 200))
        return out

    @staticmethod
    def _action_label(a: dict, deploy) -> str:
        p = a["params"]
        if a["kind"] == "rollback_deploy":
            return f"Roll back #{p.get('number', '')} on {p.get('service', '')}".strip()
        if a["kind"] == "failover_bank":
            return f"Send {p.get('bank') or p.get('from_bank') or 'affected bank'} payments to {p.get('to_bank') or 'a healthy bank'}"
        return ACTION_LABELS.get(a["kind"], a["kind"])

    def _await_decision(self, cancel) -> str:
        while True:
            if cancel.is_set():
                raise _Stop()
            if self._decision.wait(0.05):
                if cancel.is_set():
                    raise _Stop()
                return self._decision_val or "expired"
            p = self._pending
            if p and self.clock() > p["expires"]:
                return "expired"

    def _remediate(self, incident: dict, action: dict, pv: Optional[dict], cancel) -> dict:
        iid = incident["id"]
        self._set_state("remediating", action["label"])
        t0 = self._now()
        ex = {"incident_id": iid, "action_id": action["id"], "kind": action["kind"], "recovered": False}
        if pv:
            ex["predicted_recovery_ms"] = pv["expected_recovery_ms"]
        try:
            r = self.world.apply_action(action["kind"], action["params"])
            if not r.get("ok", False):
                self._error("action_failed", r.get("detail", "action failed"), True)
        except Exception as e:
            self._error("action_failed", str(e), True)
            r = {"ok": False}
        self._tl(t0, f"Approved and applied: {action['label']}")
        if action["kind"] != "notify_only" and r.get("ok", False):
            bsr = incident.get("baseline_success", 0.99)
            target = (bsr / 100.0 if bsr > 1.5 else bsr) - 0.02
            end = self.clock() + self.recovery_timeout
            while self.clock() < end:
                self._check(cancel)
                if self.world.success_rate_now(10) >= target:
                    ex["recovered"] = True
                    ex["actual_recovery_ms"] = max(0, self._now() - t0)
                    break
                if self.poll_s:
                    time.sleep(self.poll_s)
        self._emit("action.executed", ex)
        if ex["recovered"]:
            self._tl(self._now(), f"Recovered in {ex['actual_recovery_ms'] // 1000}s")
            sv = self._savings_calc(incident, t0 + ex["actual_recovery_ms"])
            self._savings = sv
            self._emit("impact.savings", dict(sv, incident_id=iid))
        return ex

    def _savings_calc(self, incident: dict, recovery_ts: int) -> dict:
        try:
            from mayday.sim.impact import compute_savings  # type: ignore
            internal = compute_savings_internal(incident, recovery_ts, self.mttr_ms)
            rate = (incident.get("blast") or {}).get("paise_per_min_at_risk", 0)
            sv = compute_savings(rate, max(0, recovery_ts - int(incident["start_ts"])), internal["actual_loss_paise"], self.mttr_ms)
            if isinstance(sv, dict) and {"actual_loss_paise", "counterfactual_loss_paise", "saved_paise"} <= set(sv):
                sv.setdefault("assumed_manual_mttr_ms", self.mttr_ms)
                return {k: sv[k] for k in ("actual_loss_paise", "counterfactual_loss_paise", "saved_paise", "assumed_manual_mttr_ms")}
        except Exception:
            pass
        return compute_savings_internal(incident, recovery_ts, self.mttr_ms)

    def _finish_inconclusive(self, incident: dict, reason: str) -> None:
        iid = incident["id"]
        part = getattr(self, "_partial", None)
        ids = [e for e in (part or {}).get("evidence_ids", []) if self.store.get(e)] or self.store.ids()[-4:]
        with self._lock:
            ruled = [{"target": h["target"], "reason": h["reason"]} for h in self._hyps.values() if h["status"] == "ruled_out"]
        rca = {"incident_id": iid, "cause": "inconclusive", "cause_label": "Inconclusive - no verified root cause",
               "confidence": 0.0, "evidence": [{"id": e, "kind": self.store.get(e)["kind"], "title": self.store.get(e)["title"]} for e in ids[:5]],
               "ruled_out": ruled, "summary": trunc(f"Could not verify a root cause ({reason}). Best partial evidence attached; escalate to a human.", 600),
               "inconclusive": True}
        with self._lock:
            self._rca = rca
        self._emit("rca.ready", {"rca": rca})
        self._set_state("inconclusive", trunc(reason, 120))
        self._last = {"incident_id": iid, "agent_cause": "inconclusive", "inconclusive": True, "verified": False,
                      "action_proposed": False, "action_kind": None, "rca": rca}
        self._partial = None

    # ------------------------------------------------------------------ voice / ask
    def _ask_run(self, qid: str, text: str, lang: str) -> None:
        try:
            from .util import detect_lang
            lg = detect_lang(text) if lang in ("auto", "", None) else lang
            self._emit("voice.query", {"query_id": qid, "transcript": text, "lang": lg})
            messages = [{"role": "system", "content": ANSWER_PROMPT},
                        {"role": "user", "content": f"QUESTION[lang={lg}]: {text}"}]
            ev_ids: list = []
            tools = specs(READ_ONLY_TOOLS)
            answer = ""
            for rnd in range(3):
                reply = self.gateway.chat(messages, tools, timeout=self.model_timeout, num_predict=300)
                messages.append({"role": "assistant", "content": reply.text or "",
                                 "tool_calls": [{"id": c.id, "function": {"name": c.name, "arguments": c.args}} for c in reply.tool_calls]})
                if not reply.tool_calls:
                    answer = reply.text
                    break
                for c in reply.tool_calls:
                    if c.name in READ_ONLY_TOOLS:
                        r = self.tools.call(c.name, c.args)
                        ev_ids += [e for e in r.evidence_ids if e not in ev_ids]
                        messages.append(self._toolmsg(c, {"ok": r.ok, "summary": r.summary, "data": r.data, "evidence_ids": r.evidence_ids}))
                    else:
                        messages.append(self._toolmsg(c, {"ok": False, "summary": "tool not allowed in Q&A", "data": {}, "evidence_ids": []}))
                if rnd == 1:   # force an answer on the last round
                    tools = None
            self._emit("voice.answer", {"query_id": qid, "text": answer or "I could not answer that from the available telemetry.",
                                        "lang": lg, "evidence_ids": ev_ids})
        except Exception as e:
            self._error("ask_failed", f"Gemma could not answer: {e}", True)
            self._emit("voice.answer", {"query_id": qid, "text": "The model is unavailable right now, so I cannot answer. Check the Gemma status.",
                                        "lang": "en", "evidence_ids": []})
