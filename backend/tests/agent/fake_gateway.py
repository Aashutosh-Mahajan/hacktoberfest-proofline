"""TEST DOUBLE ONLY (not part of the product): ScriptedGateway, a deterministic fake "model". It is a pure policy over the message history: it calls
tools, reads their results, and forms a hypothesis from the data via heuristics (no ground truth access).
`wrong_first=True` makes its first conclusion deliberately wrong (e.g. blame the latest deploy) so the
verifier-rejection -> self-correct path is demonstrable."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from mayday.agent.gateway import Gateway, ModelReply, ToolCall
from mayday.agent.util import detect_lang, trunc


def _parse(content):
    try:
        return json.loads(content) if isinstance(content, str) else (content or {})
    except Exception:
        return {}


class Facts:
    def __init__(self, messages: list):
        self.ctx: dict = {}
        self.results: dict = {}       # tool name -> list of data dicts
        self.args: dict = {}          # tool name -> list of arg dicts
        self.ev_ids: list = []
        self.calls: list = []         # (name, args) in order
        self.submits = 0
        self.seg_start = 0
        self.reject_codes: list = []  # per rejection: set of codes
        self.tried_kinds: list = []
        for i, m in enumerate(messages):
            role, c = m.get("role"), m.get("content") or ""
            if role == "user" and c.startswith("CONTEXT:") and not self.ctx:
                self.ctx = _parse(c[len("CONTEXT:"):].strip())
            elif role == "user" and c.startswith("VERIFIER_REJECTED"):
                fb = _parse(c.split("\n", 1)[1] if "\n" in c else "{}")
                self.reject_codes.append({x.get("code") for x in fb.get("checks", []) if not x.get("passed")})
                if fb.get("action_kind"):
                    self.tried_kinds.append(fb["action_kind"])
                self.seg_start = i + 1
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    name, args = fn.get("name"), fn.get("arguments") or {}
                    self.calls.append((name, args))
                    if name == "submit_conclusion":
                        self.submits += 1
            elif role == "tool":
                d = _parse(c)
                name = m.get("tool_name") or ""
                if d.get("ok"):
                    self.results.setdefault(name, []).append(d.get("data", {}))
                    self.ev_ids += [e for e in d.get("evidence_ids", []) if e not in self.ev_ids]
        self.seg_calls = []
        # calls made in the current segment (after the last rejection)
        n_rej_seen = 0
        seg = messages[self.seg_start:]
        for m in seg:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    self.seg_calls.append(fn.get("name"))

    def first(self, name):
        r = self.results.get(name)
        return r[0] if r else None

    def last(self, name):
        r = self.results.get(name)
        return r[-1] if r else None


class ScriptedGateway(Gateway):
    backend = "fake"
    name = "fake-model"

    def __init__(self, wrong_first: bool = False, latency_s: float = 0.0):
        self.wrong_first = wrong_first
        self.latency_s = latency_s
        self.calls = 0

    def chat(self, messages, tools=None, *, think=False, timeout=30.0, on_text=None, require_tool=False, **kw) -> ModelReply:
        self.calls += 1
        if self.latency_s:
            time.sleep(self.latency_s)
        sys_c = (messages[0].get("content") or "") if messages else ""
        r = self._answer(messages) if sys_c.startswith("MODE:ANSWER") else self._investigate(messages)
        r.via = "fake-model"
        return r

    # ------------------------------------------------------------------ investigation
    def _tc(self, text, *calls):
        return ModelReply(text, [ToolCall(f"call_{i}_{n}", n, a) for i, (n, a) in enumerate(calls)])

    def _investigate(self, messages) -> ModelReply:
        f = Facts(messages)
        bd, bs, dl = f.first("decline_breakdown"), f.first("bank_stats"), f.first("list_deploys")
        if bd is None:
            return self._tc("Splitting declines into technical vs business and by bank.",
                            ("decline_breakdown", {"window": "incident", "by": "bank"}))
        if bs is None:
            return self._tc(self._bd_thought(bd), ("bank_stats", {"window": "incident"}))
        worst, wd = self._worst(bs)
        if dl is None:
            return self._tc(f"{worst or 'No bank'} is the most impaired ({wd:+.1f}pp). Checking recent deploys.",
                            ("list_deploys", {"window": "recent"}))
        rows = dl.get("deploys", [])
        fetched = {a.get("deploy_id") for n, a in f.calls if n == "get_deploy"}
        cand = [r for r in rows if r["on_path"] and r["before_dip"] and r["t_vs_dip_s"] >= -900]
        for r in cand[:2]:
            if r["id"] not in fetched:
                return self._tc(f"Deploy #{r['number']} on {r['service']} landed {abs(r['t_vs_dip_s'])}s before the dip; reading its diff.",
                                ("get_deploy", {"deploy_id": r["id"]}))
        biz = (bd.get("business_pct") or 0) >= 70
        if f.first("traffic") is None:
            return self._tc("Checking whether traffic volume explains it.", ("traffic", {"window": "incident"}))
        if (not biz and worst and not cand and f.first("read_bulletin") is None):
            return self._tc(f"No on-path deploy precedes the dip; checking whether {worst} published a bulletin.",
                            ("read_bulletin", {"bank": worst}))
        if not biz and worst and f.first("query_logs") is None:
            code = next((c["code"] for c in bd.get("top_codes", []) if c["code"].startswith("TD_")), None)
            return self._tc(f"Sampling {worst} failure logs for the dominant technical code.",
                            ("query_logs", {"bank": worst, "code": code, "window": "incident", "limit": 5}))
        dg = self._diagnose(f, bd, bs, rows, cand, worst, wd)
        if not any(c == "update_hypothesis" for c in f.seg_calls):
            ups = [("update_hypothesis", u) for u in dg["updates"]]
            return self._tc(dg["thought"], *ups) if ups else self._submit(dg)
        return self._submit(dg)

    def _submit(self, dg):
        a = {"cause": dg["cause"], "confidence": dg["confidence"], "evidence_ids": dg["evidence_ids"],
             "ruled_out": dg["ruled_out"], "summary": dg["summary"]}
        if dg.get("action"):
            a["proposed_action"] = dg["action"]
        return self._tc("Submitting conclusion for verification.", ("submit_conclusion", a))

    @staticmethod
    def _worst(bs):
        worst, wd = None, -1.0
        for b, v in bs["banks"].items():
            if v["d_pp"] < wd:
                worst, wd = b, v["d_pp"]
        return worst, wd

    @staticmethod
    def _bd_thought(bd):
        top = (bd.get("top_codes") or [{}])[0]
        return f"{bd.get('technical_pct', 0):.0f}% of declines are technical; top code {top.get('code')} ({top.get('pct')}%). Checking per-bank health."

    def _diagnose(self, f, bd, bs, rows, cand, worst, wd) -> dict:
        banks = bs["banks"]
        biz = (bd.get("business_pct") or 0) >= 70 and wd > -2
        tr = (f.first("traffic") or {}).get("ratio", 1.0)
        bull = f.first("read_bulletin") or {}
        rej = set().union(*f.reject_codes) if f.reject_codes else set()
        first_attempt = f.submits == 0

        # --- genuine conclusion from data ---
        cause, action_kind = "inconclusive", None
        deploy_row = None
        if biz:
            code = next((c["code"] for c in bd.get("top_codes", []) if c["code"].startswith("BD_")), "BD_UNKNOWN")
            cause, action_kind = "business:" + code[3:].lower(), "notify_only"
        elif cand:
            deploy_row = max(cand, key=lambda r: r["t_vs_dip_s"])
            cause, action_kind = f"deploy:{deploy_row['number']}", "rollback_deploy"
        elif worst and wd <= -5:
            cause, action_kind = f"bank:{worst}", "failover_bank"
        elif tr >= 1.8:
            cause, action_kind = "traffic_spike", "scale_gateway"

        # --- deliberate wrong first conclusion (demo/test flag) ---
        wrong = False
        if self.wrong_first and first_attempt:
            latest = max(rows, key=lambda r: r["number"], default=None)
            if latest and cause != f"deploy:{latest['number']}":
                cause, action_kind, deploy_row, wrong = f"deploy:{latest['number']}", "rollback_deploy", latest, True
            elif cause.startswith("bank:") and bull.get("available") and not bull.get("consistent"):
                pass  # naive: trust the (contradicted) bulletin on the first attempt
            elif worst and cause != f"bank:{worst}":
                cause, action_kind, wrong = f"bank:{worst}", "failover_bank", True
            elif cause != "traffic_spike":
                cause, action_kind, wrong = "traffic_spike", "scale_gateway", True

        # --- action (with twin-fallback ladder) ---
        action = None
        kind = cause.split(":")[0]
        if action_kind and not wrong and "TWIN_NO_RECOVERY" in rej:
            ladder = {"deploy": ["rollback_deploy", "failover_bank"], "bank": ["failover_bank", "enable_circuit_breaker"]}.get(kind, [action_kind])
            nxt = [k for k in ladder if k not in f.tried_kinds]
            action_kind = nxt[0] if nxt else None
        if action_kind:
            others = sorted((b for b in banks if b != worst), key=lambda b: -banks[b]["tsr"])
            if action_kind == "rollback_deploy" and deploy_row:
                p = {"deploy_id": deploy_row["id"], "number": str(deploy_row["number"]), "service": deploy_row["service"]}
                rat = f"Roll back #{deploy_row['number']} on {deploy_row['service']}"
            elif action_kind == "failover_bank" and worst:
                p = {"bank": worst, "from_bank": worst, "to_bank": others[0] if others else ""}
                rat = f"Shift {worst} traffic to {others[0] if others else 'healthy banks'}"
            elif action_kind == "notify_only":
                p, rat = {}, "Nothing is broken; notify stakeholders instead of changing infrastructure"
            else:
                p, rat = {}, f"Apply {action_kind}"
            action = {"kind": action_kind, "params": p, "rationale": rat}

        # --- evidence ---
        have = set(f.ev_ids)
        ev = []

        def add(i):
            if i in have and i not in ev:
                ev.append(i)
        if kind == "deploy" and deploy_row:
            add(f"ev_deploy_{deploy_row['number']}")
        add(f"ev_bank_{worst}") if worst else None
        add("ev_breakdown_bank")
        if kind == "business":
            add("ev_metric_traffic")
        bull_ok = bool(bull.get("available")) and bull.get("consistent")
        naive_bulletin = self.wrong_first and first_attempt and bull.get("available")
        if kind == "bank" and (bull_ok or naive_bulletin) and "BULLETIN_CONTRADICTED" not in rej:
            add(f"ev_bulletin_{worst}")
        for i in f.ev_ids:
            if i.startswith("ev_log_"):
                add(i)
        if kind == "bank" and not wrong:
            pass

        # --- hypotheses ---
        updates, ruled = [], []
        for h in f.ctx.get("hypotheses", []):
            tgt = h["target"]
            if tgt == cause or (kind == "business" and h["kind"] == "business_decline") or \
               (cause == "traffic_spike" and h["kind"] == "traffic_spike"):
                updates.append({"id": h["id"], "status": "supported", "reason": trunc(self._support(cause, deploy_row, banks, worst, bd, tr), 120)})
                continue
            if wrong:
                continue
            reason = self._rule_out(h, rows, banks, bd, tr, kind, worst)
            updates.append({"id": h["id"], "status": "ruled_out", "reason": trunc(reason, 120)})
            ruled.append({"target": tgt, "reason": trunc(reason, 120)})
        summary = self._summary(cause, deploy_row, banks, worst, bd, tr, bull, wrong)
        conf = 0.8 if wrong else {"deploy": 0.93, "bank": 0.9, "business": 0.92}.get(kind, 0.7)
        if cause == "inconclusive":
            conf = 0.2
        thought = ("Naively blaming the most recent deploy." if wrong else
                   f"Evidence points to {cause}. Updating hypothesis board.")
        return {"cause": cause, "confidence": conf, "evidence_ids": ev, "ruled_out": ruled, "action": action,
                "summary": summary, "updates": updates, "thought": thought}

    @staticmethod
    def _support(cause, row, banks, worst, bd, tr):
        if cause.startswith("deploy") and row:
            return f"#{row['number']} landed {abs(row['t_vs_dip_s'])}s before the dip on {row['service']}; {worst} timeouts follow"
        if cause.startswith("bank"):
            b = banks[worst]
            return f"{worst} tech success {b['tsr']:.1%} vs {b['base_tsr']:.1%}; 5xx {b['err5xx_pct']}%, p95 {b['p95']}ms"
        if cause.startswith("business"):
            return f"{bd.get('business_pct', 0):.0f}% of failures are business declines; technical success at baseline"
        return f"traffic is {tr}x baseline"

    @staticmethod
    def _rule_out(h, rows, banks, bd, tr, kind, worst):
        k, tgt = h["kind"], h["target"]
        if k == "deploy":
            n = tgt.split(":")[1]
            r = next((x for x in rows if str(x["number"]) == n), None)
            if r and r["t_vs_dip_s"] > 15:
                return f"#{n} ({r['service']}) landed {r['t_vs_dip_s']}s AFTER the dip began" + ("" if r["on_path"] else "; off payment path")
            if r and not r["on_path"]:
                return f"#{n} is on {r['service']}, off the payment path"
            return f"#{n} has no bank-specific effect matching the failures"
        if k == "bank_outage":
            b = banks.get(tgt.split(":")[1], {})
            if kind == "deploy":
                return f"{tgt.split(':')[1]} 5xx {b.get('err5xx_pct')}%, p95 {b.get('p95')}ms: healthy; failures are our own timeouts"
            return f"{tgt.split(':')[1]} tech success {b.get('tsr', 0):.1%} vs {b.get('base_tsr', 0):.1%} baseline: healthy"
        if k == "traffic_spike":
            return f"traffic {tr}x baseline: no surge"
        if k == "business_decline":
            return f"business declines only {bd.get('business_pct', 0):.0f}% of failures"
        return "no supporting evidence"

    @staticmethod
    def _summary(cause, row, banks, worst, bd, tr, bull, wrong):
        if wrong:
            return f"Most recent deploy is the likely cause ({cause})."
        if cause.startswith("deploy") and row:
            b = banks.get(worst, {})
            return trunc(f"Deploy #{row['number']} on {row['service']} landed {abs(row['t_vs_dip_s'])}s before the dip; {worst} technical success "
                         f"fell to {b.get('tsr', 0):.0%} while other banks stayed at baseline. {bd.get('technical_pct', 0):.0f}% of declines are technical.", 400)
        if cause.startswith("bank"):
            b = banks[worst]
            s = (f"{worst} is degraded (tech success {b['tsr']:.0%}, p95 {b['p95']}ms, 5xx {b['err5xx_pct']}%) with no on-path deploy before the dip; other banks healthy.")
            if bull.get("available"):
                s += " Bank bulletin matches telemetry." if bull.get("consistent") else " Bank bulletin contradicted by telemetry; not relied on."
            return trunc(s, 400)
        if cause.startswith("business"):
            return trunc(f"{bd.get('business_pct', 0):.0f}% of failures are business declines ({cause.split(':')[1].replace('_', ' ')}); infrastructure is healthy. "
                         "No rollback needed; notify stakeholders.", 400)
        if cause == "traffic_spike":
            return f"Traffic is {tr}x baseline."
        return "Evidence does not isolate a single cause."

    # ------------------------------------------------------------------ Q&A
    def _answer(self, messages) -> ModelReply:
        q_msg = next((m["content"] for m in messages if m.get("role") == "user" and m["content"].startswith("QUESTION")), "")
        m = re.match(r"QUESTION\[lang=(\w+)\]:\s*(.*)", q_msg, re.S)
        lang, q = (m.group(1), m.group(2)) if m else ("en", q_msg)
        if lang in ("auto", ""):
            lang = detect_lang(q)
        f = Facts(messages)
        ql = q.lower()
        banks = re.findall(r"bank[_ ]?([xyz])", ql)
        bank = f"BANK_{banks[0].upper()}" if banks else None
        if not f.results:
            if re.search(r"deploy|rollback|release|push", ql):
                return self._tc("Looking at deploys.", ("list_deploys", {"window": "recent"}))
            if re.search(r"traffic|load|tps", ql):
                return self._tc("Checking traffic.", ("traffic", {"window": "incident"}))
            if re.search(r"decline|reason|wajah|kyun|why", ql) and not bank:
                return self._tc("Checking declines.", ("decline_breakdown", {"window": "incident"}))
            return self._tc("Checking bank health.", ("bank_stats", {"window": "incident"}))
        ids = f.ev_ids
        if (r := f.last("bank_stats")):
            bs = r["banks"]
            if bank and bank in bs:
                v = bs[bank]
                bad = v["d_pp"] <= -5
                if lang == "hi":
                    t = (f"{bank} की तकनीकी सफलता दर {v['tsr']:.1%} है (सामान्य {v['base_tsr']:.1%})। " + ("यहाँ गंभीर असर है।" if bad else "कोई असर नहीं दिख रहा।"))
                elif lang == "hinglish":
                    t = (f"{bank} ki technical success rate {v['tsr']:.1%} hai (normal {v['base_tsr']:.1%}). " + ("Yahan asar hua hai." if bad else "Koi asar nahi dikh raha."))
                else:
                    t = (f"{bank} technical success is {v['tsr']:.1%} vs {v['base_tsr']:.1%} baseline, p95 {v['p95']}ms. " + ("It is clearly impacted." if bad else "No impact seen."))
            else:
                worst, wd = self._worst(r)
                if lang == "hi":
                    t = f"सबसे ज़्यादा प्रभावित: {worst} ({wd:+.1f}pp)।" if worst else "सभी बैंक सामान्य हैं।"
                elif lang == "hinglish":
                    t = f"Sabse zyada asar {worst} par hai ({wd:+.1f}pp)." if worst else "Saare banks normal hain."
                else:
                    t = f"Most impacted: {worst} ({wd:+.1f}pp vs baseline)." if worst else "All banks are within baseline."
        elif (r := f.last("list_deploys")):
            d = r.get("deploys", [])
            t = ("Recent deploys: " + "; ".join(f"#{x['number']} {x['service']} ({x['t_vs_dip_s']:+d}s vs dip)" for x in d[:3])) if d else "No recent deploys."
            if lang != "en":
                t = ("हाल के डिप्लॉय: " if lang == "hi" else "Recent deploys: ") + "; ".join(f"#{x['number']} {x['service']}" for x in d[:3])
        elif (r := f.last("traffic")):
            t = f"Traffic is {r['ratio']}x baseline."
        else:
            r = f.last("decline_breakdown") or {}
            t = f"{r.get('technical_pct', 0):.0f}% technical vs {r.get('business_pct', 0):.0f}% business declines."
        return ModelReply(t, [])
