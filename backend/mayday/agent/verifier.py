"""Deterministic verifier (PRD FR-19..23). The model proposes; code decides.

Each rule returns a Check(name, passed, code, detail). `code` is a machine-readable reason
("OK" when passed). Rules that do not apply to the claimed cause are omitted.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from .util import DEPLOY_LOOKBACK_MS, DEPLOY_TOLERANCE_MS, on_path

BULLETIN_TOL_MS = 30_000
ACTION_FOR = {"deploy": ("rollback_deploy",), "bank": ("notify_only", "enable_circuit_breaker"), "business": ("notify_only",),
              "traffic_spike": ("scale_gateway", "enable_circuit_breaker")}


@dataclass
class Check:
    name: str
    passed: bool
    code: str
    detail: str

    def to_contract(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}

    def to_feedback(self) -> dict:
        return {"rule": self.name, "passed": self.passed, "code": self.code, "detail": self.detail}


@dataclass
class VerifyResult:
    checks: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list:
        return [c for c in self.checks if not c.passed]

    @property
    def rejected_reason(self) -> Optional[str]:
        f = self.failed
        return "; ".join(f"{c.name}: {c.detail}" for c in f)[:300] if f else None


def bulletin_consistency(extracted: dict, truth: Optional[tuple], now: int) -> tuple:
    """Compare a bulletin's claimed window with the telemetry-derived failure window."""
    if not truth:
        return False, "no elevated-failure window in telemetry to compare against"
    ts, te = truth
    cs, ce = extracted.get("claimed_start_ts"), extracted.get("claimed_end_ts")
    if cs is None or ce is None:
        return False, "bulletin has no claimed window"
    if abs(cs - ts) > BULLETIN_TOL_MS:
        return False, f"claimed start differs from telemetry by {abs(cs - ts) // 1000}s"
    if ce < te - BULLETIN_TOL_MS:
        return False, f"bulletin says issue ended {(te - ce) // 1000}s before telemetry shows failures stopping"
    return True, "claimed window matches telemetry"


def parse_cause(cause: str) -> tuple:
    c = (cause or "").strip()
    if c.startswith("deploy:"):
        return "deploy", c.split(":", 1)[1].lstrip("#")
    if c.startswith("bank:"):
        return "bank", c.split(":", 1)[1]
    if c.startswith("business:"):
        return "business", c.split(":", 1)[1]
    if c == "traffic_spike":
        return "traffic_spike", ""
    return "other", c


class Verifier:
    def __init__(self, telemetry, store):
        self.tel = telemetry
        self.store = store
        self.catches = 0
        self.rule_catches: dict = {}
        self._lock = threading.Lock()

    # ---- data helpers ----
    def _windows(self, incident):
        s = int(incident["start_ts"])
        now = self.tel.now()
        return (s, max(now, s + 1000)), (s - 300_000, s - 15_000)

    def _bank_delta(self, incident):
        (s, e), (bs, be) = self._windows(incident)
        cur, base = self.tel.bank_stats(s, e), self.tel.bank_stats(bs, be)
        d = {b: (cur[b].get("technical_success_rate", 0) - base.get(b, {}).get("technical_success_rate", 0)) * 100
             for b in cur}
        tot = sum(cur[b].get("tps", 0) for b in cur) or 1
        overall = sum(d[b] * cur[b].get("tps", 0) for b in cur) / tot
        return d, overall

    def _find_deploy(self, number, incident):
        now = self.tel.now()
        for d in self.tel.deploys(int(incident["start_ts"]) - 6 * 3600_000, now + 1):
            if str(d["number"]) == str(number):
                return d
        return None

    def _recent_deploys(self, incident):
        s, now = int(incident["start_ts"]), self.tel.now()
        return self.tel.deploys(s - DEPLOY_LOOKBACK_MS, now + 1)

    # ---- main entry ----
    def verify(self, conclusion: dict, incident: dict, preview: Optional[dict] = None) -> VerifyResult:
        kind, ref = parse_cause(conclusion.get("cause", ""))
        res = VerifyResult()
        add = res.checks.append
        if kind == "other":
            add(Check("cause_supported", False, "UNKNOWN_CAUSE", "cause must be a supported, evidence-backed explanation"))
        add(self._evidence_exists(conclusion, kind, ref))
        for fn in (self._deploy_precedes_dip, self._deploy_on_failing_path, self._scope_concentration,
                   self._others_healthy, self._no_internal_cause, self._traffic_elevated,
                   self._action_matches_cause, self._decline_class_matches, self._external_claim_consistent):
            c = fn(conclusion, kind, ref, incident)
            if c is not None:
                add(c)
        if preview is not None:
            add(self.twin_check(preview))
        if not res.passed:
            with self._lock:
                self.catches += 1
                for c in res.failed:
                    self.rule_catches[c.name] = self.rule_catches.get(c.name, 0) + 1
        return res

    def append_check(self, res: "VerifyResult", chk: Check) -> None:
        """Add a late check (twin) and count a catch if it flips a passing result to failing."""
        was = res.passed
        res.checks.append(chk)
        if was and not chk.passed:
            with self._lock:
                self.catches += 1
                self.rule_catches[chk.name] = self.rule_catches.get(chk.name, 0) + 1

    # ---- rules ----
    def _evidence_exists(self, c, kind, ref) -> Check:
        ids = c.get("evidence_ids") or []
        missing = [i for i in ids if not self.store.has(i)]
        if not ids:
            return Check("evidence_exists", False, "NO_EVIDENCE", "conclusion cites no evidence")
        if missing:
            return Check("evidence_exists", False, "EVIDENCE_NOT_FOUND", f"unknown evidence ids: {', '.join(missing)}")
        if kind == "deploy" and f"ev_deploy_{ref}" not in ids:
            return Check("evidence_exists", False, "MISSING_DEPLOY_EVIDENCE", f"deploy #{ref} claimed but ev_deploy_{ref} not cited")
        if kind == "bank" and not any(i in ids for i in (f"ev_bank_{ref}",)) :
            return Check("evidence_exists", False, "MISSING_BANK_EVIDENCE", f"{ref} claimed but ev_bank_{ref} not cited")
        if kind == "traffic_spike" and "ev_metric_traffic" not in ids:
            return Check("evidence_exists", False, "MISSING_TRAFFIC_EVIDENCE", "traffic surge claimed without traffic evidence")
        return Check("evidence_exists", True, "OK", f"{len(ids)} evidence ids resolve")

    def _deploy_precedes_dip(self, c, kind, ref, inc) -> Optional[Check]:
        if kind != "deploy":
            return None
        d = self._find_deploy(ref, inc)
        if not d:
            return Check("deploy_precedes_dip", False, "DEPLOY_UNKNOWN", f"deploy #{ref} not found")
        dt = (d["ts"] - int(inc["start_ts"])) / 1000
        if d["ts"] <= int(inc["start_ts"]) + DEPLOY_TOLERANCE_MS:
            return Check("deploy_precedes_dip", True, "OK", f"deploy #{ref} landed {abs(dt):.0f}s {'before' if dt <= 0 else 'after'} dip start (within tolerance)")
        return Check("deploy_precedes_dip", False, "DEPLOY_AFTER_DIP", f"deploy #{ref} landed {dt:.0f}s AFTER the dip began; cannot be the cause")

    def _deploy_on_failing_path(self, c, kind, ref, inc) -> Optional[Check]:
        if kind != "deploy":
            return None
        d = self._find_deploy(ref, inc)
        if not d:
            return Check("deploy_on_failing_path", False, "DEPLOY_UNKNOWN", f"deploy #{ref} not found")
        ok = on_path(d["service"])
        return Check("deploy_on_failing_path", ok, "OK" if ok else "DEPLOY_OFF_PATH",
                     f"{d['service']} is {'on' if ok else 'not on'} the payment path")

    def _scope_concentration(self, c, kind, ref, inc) -> Optional[Check]:
        if kind not in ("deploy", "bank"):
            return None
        d, _ = self._bank_delta(inc)
        if not d:
            return Check("scope_concentration", False, "NO_DATA", "no bank data")
        worst = min(d, key=d.get)
        bank = ref if kind == "bank" else None
        if kind == "deploy":
            dep = self._find_deploy(ref, inc)
            m = re.findall(r"BANK_[XYZ]", (dep or {}).get("diff", "") + (dep or {}).get("summary", ""))
            bank = m[0] if m else None
            if bank is None:
                return Check("scope_concentration", True, "OK", "deploy not bank-specific; scope check skipped")
        if bank not in d:
            return Check("scope_concentration", False, "UNKNOWN_BANK", f"{bank} not in telemetry")
        others = [v for b, v in d.items() if b != bank]
        if d[bank] > -5:
            return Check("scope_concentration", False, "NOT_DEGRADED", f"{bank} technical success only {d[bank]:+.1f}pp vs baseline")
        if worst != bank:
            return Check("scope_concentration", False, "WRONG_SCOPE", f"{worst} is more degraded than claimed {bank}")
        if others and min(others) < d[bank] / 2:
            return Check("scope_concentration", False, "NOT_CONCENTRATED", "failures are not concentrated in the claimed bank")
        return Check("scope_concentration", True, "OK", f"{bank} {d[bank]:+.1f}pp; others within {max(others, default=0):+.1f}..{min(others, default=0):+.1f}pp")

    def _others_healthy(self, c, kind, ref, inc) -> Optional[Check]:
        if kind not in ("deploy", "bank"):
            return None
        d, _ = self._bank_delta(inc)
        bank = ref if kind == "bank" else None
        if kind == "deploy":
            dep = self._find_deploy(ref, inc)
            m = re.findall(r"BANK_[XYZ]", (dep or {}).get("diff", "") + (dep or {}).get("summary", ""))
            bank = m[0] if m else None
            if bank is None:
                return None
        sick = {b: round(v, 1) for b, v in d.items() if b != bank and v < -3}
        return Check("others_healthy", not sick, "OK" if not sick else "OTHERS_DEGRADED",
                     "other banks within baseline" if not sick else f"other banks degraded: {sick}")

    def _no_internal_cause(self, c, kind, ref, inc) -> Optional[Check]:
        if kind not in ("bank", "business", "traffic_spike"):
            return None
        s = int(inc["start_ts"])
        deps = self._recent_deploys(inc)
        culprits = [d for d in deps if d["ts"] <= s + DEPLOY_TOLERANCE_MS and on_path(d["service"])]
        if culprits:
            d = culprits[0]
            return Check("no_internal_cause", False, "INTERNAL_DEPLOY_PRECEDES_DIP",
                         f"deploy #{d['number']} on {d['service']} landed before the dip; rule it out before blaming outside causes")
        ruled = {(r.get("target") or "") for r in (c.get("ruled_out") or [])}
        unruled = [d for d in deps if f"deploy:{d['number']}" not in ruled]
        if unruled:
            return Check("no_internal_cause", False, "DEPLOYS_NOT_RULED_OUT",
                         "deploys not explicitly ruled out: " + ", ".join(f"#{d['number']}" for d in unruled))
        return Check("no_internal_cause", True, "OK", "no on-path deploy precedes the dip; all recent deploys ruled out")

    def _traffic_elevated(self, c, kind, ref, inc) -> Optional[Check]:
        if kind != "traffic_spike":
            return None
        (s, e), _ = self._windows(inc)
        r = self.tel.traffic(s, e).get("ratio", 1.0)
        return Check("traffic_elevated", r >= 1.8, "OK" if r >= 1.8 else "TRAFFIC_NOT_ELEVATED", f"traffic is {r:.2f}x baseline")

    def _action_matches_cause(self, c, kind, ref, inc) -> Optional[Check]:
        a = c.get("proposed_action")
        if not a:
            return None  # "no safe action" is a legitimate outcome; twin rule handles safety
        ok_kinds = ACTION_FOR.get(kind, ())
        if a.get("kind") not in ok_kinds:
            return Check("action_matches_cause", False, "ACTION_MISMATCH", f"{a.get('kind')} does not address cause {c.get('cause')}")
        p = a.get("params") or {}
        if kind == "deploy" and a.get("kind") == "rollback_deploy" and str(p.get("number", p.get("deploy_id", ""))).lstrip("#").replace("dep_", "").replace("deploy_", "") != str(ref):
            return Check("action_matches_cause", False, "ACTION_TARGET_MISMATCH", f"rollback targets {p} not deploy #{ref}")
        if kind == "bank" and a.get("kind") == "failover_bank" and p.get("bank", p.get("from_bank")) != ref:
            return Check("action_matches_cause", False, "ACTION_TARGET_MISMATCH", f"failover targets {p} not {ref}")
        return Check("action_matches_cause", True, "OK", f"{a.get('kind')} addresses {c.get('cause')}")

    def _decline_class_matches(self, c, kind, ref, inc) -> Optional[Check]:
        if kind == "other":
            return None
        (s, e), _ = self._windows(inc)
        b = self.tel.decline_breakdown(s, e, None)
        _, overall = self._bank_delta(inc)
        if kind == "business":
            ok = b.get("business_pct", 0) >= 70 and overall > -2
            return Check("decline_class_matches", ok, "OK" if ok else "NOT_BUSINESS_DECLINES",
                         f"business {b.get('business_pct', 0):.0f}%, technical success {overall:+.1f}pp vs baseline")
        ok = b.get("technical_pct", 0) >= 70 and overall <= -2
        return Check("decline_class_matches", ok, "OK" if ok else "NOT_TECHNICAL_DECLINES",
                     f"technical {b.get('technical_pct', 0):.0f}%, technical success {overall:+.1f}pp vs baseline")

    def _external_claim_consistent(self, c, kind, ref, inc) -> Optional[Check]:
        bad, seen = [], 0
        for i in c.get("evidence_ids") or []:
            ev = self.store.get(i)
            if not ev or ev.get("kind") != "bulletin":
                continue
            seen += 1
            ex = ((ev.get("body") or {}).get("bulletin") or {}).get("extracted") or {}
            bank = ex.get("bank") or i.replace("ev_bulletin_", "")
            ok, detail = bulletin_consistency(ex, self.tel.bulletin_truth_window(bank), self.tel.now())
            if not ok:
                bad.append(f"{bank}: {detail}")
        if not seen:
            return None
        return Check("external_claim_consistent", not bad, "OK" if not bad else "BULLETIN_CONTRADICTED",
                     "bulletin claims match telemetry" if not bad else "; ".join(bad))

    def twin_check(self, preview: dict) -> Check:
        rp = preview.get("recovery_pct")
        ok = bool(preview.get("recovers")) and (rp is None or rp >= 80)
        if ok:
            return Check("twin_predicts_recovery", True, "OK", f"twin predicts recovery in ~{preview.get('expected_recovery_ms', 0) // 1000}s")
        why = (f"twin shows only partial recovery ({rp:.0f}%)" if rp is not None and preview.get("recovers") else
               "twin shows this action does not restore health")
        w = (preview.get("warnings") or [])
        return Check("twin_predicts_recovery", False, "TWIN_NO_RECOVERY", why + (f" ({w[0]})" if w else ""))

    HEADROOM_MIN_PCT = 15.0

    def failover_check(self, action: dict, preview: Optional[dict], incident: dict) -> Check:
        """failover_target_healthy: the receiving bank must be healthy and have spare capacity."""
        p = (action or {}).get("params") or {}
        pv = preview or {}
        src = p.get("bank") or p.get("from_bank")
        to = p.get("to_bank") or pv.get("target_bank")
        if not to:
            return Check("failover_target_healthy", False, "NO_TARGET_BANK", "no healthy target bank to send payments to")
        if src not in ("BANK_X", "BANK_Y", "BANK_Z") or to not in ("BANK_X", "BANK_Y", "BANK_Z"):
            return Check("failover_target_healthy", False, "UNKNOWN_BANK", "source and receiving bank must exist in payment data")
        if to == src:
            return Check("failover_target_healthy", False, "TARGET_IS_SOURCE", f"target {to} is the failing bank")
        try:
            d, _ = self._bank_delta(incident)
        except Exception:
            d = {}
        if to not in d:
            return Check("failover_target_healthy", False, "NO_BANK_DATA", "cannot verify the receiving bank is healthy")
        if d[to] < -3:
            return Check("failover_target_healthy", False, "TARGET_DEGRADED", f"target {to} is itself degraded ({d[to]:+.1f}pp)")
        head = pv.get("target_headroom_pct")
        if head is not None and head < self.HEADROOM_MIN_PCT:
            return Check("failover_target_healthy", False, "TARGET_OVERLOAD", f"{to} would have only {head:.0f}% spare capacity (need >= {self.HEADROOM_MIN_PCT:.0f}%)")
        return Check("failover_target_healthy", True, "OK",
                     f"{to} healthy" + (f", ~{head:.0f}% spare capacity" if head is not None else ", spare capacity not reported by twin"))
