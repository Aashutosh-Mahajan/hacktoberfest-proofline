"""Orchestrator: bounded state machine TRIAGE -> INVESTIGATE -> CONCLUDE -> VERIFY -> PREVIEW -> PROPOSE ->
AWAIT_APPROVAL -> REMEDIATE -> COMMS -> POSTMORTEM. Model-agnostic; never raises into the caller."""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import uuid
from typing import Optional

from .comms import draft_comms
from .evidence import EvidenceStore
from .gateway import Gateway, ModelError
from .postmortem import build_postmortem
from .tools import CONTROL_TOOLS, READ_ONLY_TOOLS, TELEMETRY_TOOLS, Toolbox, specs
from .util import ASSUMED_MANUAL_MTTR_MS, AVG_TICKET_PAISE, DEPLOY_LOOKBACK_MS, trunc
from .verifier import Verifier, parse_cause

SYSTEM_PROMPT = """You are Proofline, an on-call SRE agent for a UPI payments platform. Investigate a success-rate dip with tools.
Rules: 1) Split technical vs business declines first. 2) Check bank health, deploys (timing vs dip start AND whether the service is on the payment path), traffic. 3) Record every hypothesis outcome with update_hypothesis (reason <=120 chars). 4) Finish with submit_conclusion: cause as deploy:<number> | bank:<BANK> | business:<code> | traffic_spike | inconclusive; cite evidence ids returned by tools; list ruled_out deploys/hypotheses with reasons; propose one action: rollback_deploy {deploy_id, number, service} | failover_bank {bank, to_bank} | enable_circuit_breaker | scale_gateway | notify_only. Business-decline surges get notify_only, never rollback. A deterministic verifier will check your conclusion; max 10 tool calls."""

ANSWER_PROMPT = "MODE:ANSWER. You are Proofline answering an on-call engineer's follow-up. Use read-only tools, answer briefly in the language asked (English, Hindi, or Hinglish)."

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

    # ------------------------------------------------------------------ public API
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
            self._error("audio_unsupported", "This model gateway cannot take audio. Use browser speech-to-text and POST /api/ask.", True)
            return qid

        def go():
            try:
                r = self.gateway.transcribe(audio, mime)
                self._ask_run(qid, r.get("transcript", ""), r.get("lang") or "auto")
            except Exception as e:
                self._error("audio_failed", f"{e}. Use browser speech-to-text and POST /api/ask.", True)
        threading.Thread(target=go, daemon=True).start()
        return qid

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
        self._tl(int(incident.get("detected_ts", now)), "Detected by Proofline")
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
            incident["blast"] = {"scope": f"{worst} via payment-orchestrator" if d.get(worst, 0) < -0.05 else "payments (all banks)",
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
        self._emit("agent.thought", {"incident_id": incident["id"], "text": trunc(
            f"Triage: {bd.get('technical_pct', 0):.0f}% technical / {bd.get('business_pct', 0):.0f}% business declines; "
            f"scope {incident['blast']['scope']}; {len(deploys)} recent deploy(s).", 300)})
        self._ctx = {"incident": {k: incident.get(k) for k in ("id", "start_ts", "detected_ts", "baseline_success", "current_success", "blast")},
                     "triage": incident["decline_breakdown"], "hypotheses": hyps}

    # ---- INVESTIGATE / CONCLUDE / VERIFY loop
    def _model(self, messages: list, tools: list, cancel, think: bool = False):
        for attempt in (1, 2):
            self._check(cancel)
            rem = self._remaining()
            if rem <= 0:
                raise _Inconclusive("90s time budget exhausted")
            try:
                return self.gateway.chat(messages, tools, think=think, timeout=min(self.model_timeout, rem), require_tool=True)
            except ModelError as e:
                if attempt == 2:
                    raise _ModelFail(str(e))
        raise _ModelFail("model failed")

    def _reason(self, incident: dict, cancel) -> tuple:
        iid = incident["id"]
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "CONTEXT:\n" + json.dumps(self._ctx, default=str)}]
        tools = specs(TELEMETRY_TOOLS + CONTROL_TOOLS)
        used = verify_attempts = nudges = over_budget_turns = 0
        self._set_state("investigating")
        last_concl = None
        n_call = 0
        while True:
            reply = self._model(messages, tools, cancel, think=used >= 4 or verify_attempts > 0)
            self._check(cancel)
            if reply.text:
                self._emit("agent.thought", {"incident_id": iid, "text": trunc(reply.text, 300)})
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
            for c in calls:
                if concl is not None:
                    messages.append(self._toolmsg(c, {"ok": False, "summary": "ignored after conclusion", "data": {}, "evidence_ids": []}))
                    continue
                if c.name == "update_hypothesis":
                    messages.append(self._toolmsg(c, self._update_hyp(iid, c.args)))
                elif c.name == "submit_conclusion":
                    concl = self._norm_conclusion(c.args)
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
                    messages.append({"role": "user", "content": "TOOL BUDGET EXHAUSTED. Call update_hypothesis/submit_conclusion now."})
                continue
            # ---- conclude + verify
            last_concl = concl
            self._set_state("concluding")
            if concl["cause"] == "inconclusive":
                raise _Inconclusive(concl.get("summary") or "model reported inconclusive")
            verify_attempts += 1
            res, preview, action = self._verify(incident, concl, verify_attempts)
            if res.passed:
                return concl, {"preview": preview, "action": action}
            if verify_attempts >= self.max_verify:
                self._partial = concl
                raise _Inconclusive("verifier rejected all conclusions: " + (res.rejected_reason or ""))
            self._reopen_claimed(iid, concl)
            self._set_state("investigating", "verifier rejected; self-correcting")
            fb = {"checks": [c.to_feedback() for c in res.checks], "action_kind": (concl.get("proposed_action") or {}).get("kind"),
                  "instruction": "Fix the failed checks. Rule out wrongly blamed causes with update_hypothesis, then resubmit."}
            messages.append({"role": "user", "content": "VERIFIER_REJECTED\n" + json.dumps(fb)})
            self._partial = concl

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

    def _norm_conclusion(self, a: dict) -> dict:
        a = a or {}
        ev = a.get("evidence_ids") or []
        if isinstance(ev, str):
            ev = [ev]
        ro = [{"target": str(r.get("target", "")), "reason": trunc(r.get("reason", ""), 120)} for r in (a.get("ruled_out") or []) if isinstance(r, dict)]
        act = a.get("proposed_action")
        if isinstance(act, dict) and act.get("kind"):
            act = {"kind": act["kind"], "params": {k: str(v) for k, v in (act.get("params") or {}).items()},
                   "rationale": trunc(act.get("rationale", ""), 200)}
        else:
            act = None
        try:
            conf = max(0.0, min(1.0, float(a.get("confidence", 0.5))))
        except Exception:
            conf = 0.5
        return {"cause": str(a.get("cause", "inconclusive")).strip(), "confidence": conf, "evidence_ids": [str(e) for e in ev],
                "ruled_out": ro, "proposed_action": act, "summary": trunc(a.get("summary", ""), 600)}

    def _verify(self, incident: dict, concl: dict, attempt: int) -> tuple:
        iid = incident["id"]
        self._set_state("verifying", f"attempt {attempt}/{self.max_verify}")
        res = self.verifier.verify(concl, incident)
        act = concl.get("proposed_action")
        preview = None
        if res.passed and act and act["kind"] != "notify_only":
            self._set_state("previewing", "running fix in digital twin")
            try:
                pv = self.world.preview_action(act["kind"], act["params"], 120)
                preview = {"recovers": bool(pv.get("recovers")), "expected_recovery_ms": int(pv.get("expected_recovery_ms", 0)),
                           "predicted": pv.get("predicted", []), "baseline": pv.get("baseline", [])}
            except Exception as e:
                self._error("twin_failed", str(e))
                preview = {"recovers": False, "expected_recovery_ms": 0, "predicted": [], "baseline": []}
            self._emit("twin.preview", {"incident_id": iid, "action_id": f"act_{iid}_{attempt}", "kind": act["kind"],
                                        "recovers": preview["recovers"], "expected_recovery_ms": preview["expected_recovery_ms"],
                                        "predicted": preview["predicted"], "baseline": preview["baseline"]})
            self.verifier.append_check(res, self.verifier.twin_check(preview))
        d = {"incident_id": iid, "attempt": attempt, "passed": res.passed, "checks": [c.to_contract() for c in res.checks]}
        if not res.passed:
            d["rejected_reason"] = res.rejected_reason
        self._emit("verify.result", d)
        self._tl(self._now(), f"Verify attempt {attempt}: {'passed' if res.passed else 'rejected - ' + (res.rejected_reason or '')[:100]}")
        return res, preview, act

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
        rca = {"incident_id": iid, "cause": concl["cause"], "cause_label": cause_label(concl["cause"], deploy),
               "confidence": concl["confidence"],
               "evidence": [{"id": e, "kind": self.store.get(e)["kind"], "title": self.store.get(e)["title"]} for e in concl["evidence_ids"] if self.store.get(e)],
               "ruled_out": concl["ruled_out"], "summary": concl["summary"]}
        if not action:
            rca["summary"] = trunc(rca["summary"] + " No safe automated action was found.", 600)
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
                      "action_proposed": action is not None, "action_kind": action["kind"] if action else None, "rca": rca}
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
    def _action_label(a: dict, deploy) -> str:
        p = a["params"]
        if a["kind"] == "rollback_deploy":
            return f"Roll back #{p.get('number', '')} on {p.get('service', '')}".strip()
        if a["kind"] == "failover_bank":
            return f"Fail over {p.get('bank', '')} traffic to {p.get('to_bank', 'healthy banks')}"
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
            for _ in range(5):
                reply = self.gateway.chat(messages, tools, timeout=self.model_timeout)
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
            self._emit("voice.answer", {"query_id": qid, "text": answer or "I could not answer that from the available telemetry.",
                                        "lang": lg, "evidence_ids": ev_ids})
        except Exception as e:
            self._error("ask_failed", str(e), True)
