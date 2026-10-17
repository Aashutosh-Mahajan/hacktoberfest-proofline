"""Token-lean rendering of context, tool results and history for small local models."""
from __future__ import annotations

import json
from typing import Any

from .util import parse_json_loose, trunc


def est_tokens(s: str) -> int:
    return max(1, int(len(s) / 3.4))


def _j(o: Any, n: int = 500) -> str:
    return trunc(json.dumps(o, separators=(",", ":"), default=str), n)


def render_context(ctx: dict) -> str:
    inc = ctx.get("incident", {}) or {}
    bl = inc.get("blast") or {}
    tr = ctx.get("triage") or {}
    codes = ",".join(f"{c.get('code')} {c.get('pct')}%" for c in (tr.get("top_codes") or [])[:3])
    hyp = "\n".join(f"- {h['id']} = {h['target']}" for h in ctx.get("hypotheses", []))
    bs, cs = inc.get("baseline_success"), inc.get("current_success")
    pct = lambda v: f"{v * 100:.0f}%" if isinstance(v, (int, float)) and v <= 1.5 else f"{v}%"
    return (f"INCIDENT {inc.get('id')}: success {pct(bs)} -> {pct(cs)}; scope {bl.get('scope')}; "
            f"declines {tr.get('technical_pct')}% technical / {tr.get('business_pct')}% business (top {codes}).\n"
            f"{('Degraded bank at triage: ' + str(ctx['worst_bank']['bank']) + ' (' + str(ctx['worst_bank']['d_pp']) + 'pp technical success).' + chr(10)) if ctx.get('worst_bank') else ''}"
            f"Bank changes vs normal (percentage points): {_j(ctx.get('bank_changes_pp', {}))}. Several negative values mean a shared problem is possible; do not blame the slightly worst bank.\n"
            f"Measured traffic: {_j(ctx.get('traffic', {}))} [ev:ev_metric_traffic]. Ratio>=1.8 plus widespread excess-request errors supports traffic_spike; test scale_gateway.\n"
            f"Hypothesis targets (rule out or confirm each in submit_conclusion):\n{hyp}")


def render_result(tool: str, p: dict, full: bool = True) -> str:
    ev = ",".join(p.get("evidence_ids") or [])
    evs = f" [ev:{ev}]" if ev else ""
    if not p.get("ok", True):
        return f"ERROR {p.get('summary', '')}"
    if not full:
        return trunc(p.get("summary", ""), 140) + evs
    d = p.get("data") or {}
    try:
        if tool == "bank_stats":
            rows = [f"{b} tsr={v['tsr']} base={v['base_tsr']} d={v['d_pp']}pp p95={v['p95']}(base {v['base_p95']}) 5xx={v['err5xx_pct']}% tps={v['tps']}"
                    for b, v in d["banks"].items()]
            return "\n".join(rows) + evs
        if tool == "decline_breakdown":
            codes = ",".join(f"{c.get('code')} {c.get('pct')}%" for c in d.get("top_codes", []))
            return f"technical {d.get('technical_pct')}% business {d.get('business_pct')}%; top {codes}" + evs
        if tool == "list_deploys":
            rows = [f"#{r['number']} {r['service']} t={r['t_vs_dip_s']:+d}s before_dip={r['before_dip']} on_path={r['on_path']}"
                    f"{' banks=' + ','.join(r['banks']) if r.get('banks') else ''} \"{r['summary']}\" id={r['id']}" for r in d.get("deploys", [])]
            return "\n".join(rows) + evs
        if tool == "traffic":
            return f"traffic {d.get('ratio')}x baseline ({d.get('baseline_tps')} tps)" + evs
        if tool == "read_bulletin":
            if not d.get("available"):
                return "no bulletin published"
            e = d.get("extracted", {})
            return f"bulletin {e.get('bank')} status={e.get('status')} consistent_with_telemetry={d.get('consistent')} ({d.get('detail')})" + evs
        if tool == "query_logs":
            ls = "; ".join(f"{l.get('svc')}/{l.get('bank')}/{l.get('code')}: {l.get('msg')}" for l in d.get("lines", [])[:4])
            return f"{d.get('total')} lines. {ls}" + evs
    except Exception:
        pass
    return trunc(p.get("summary", ""), 140) + " " + _j(d, 450) + evs


def compact_messages(messages: list, budget_tokens: int = 2400) -> list:
    """Messages in the internal format -> lean Ollama messages. Old tool results collapse to summaries when over budget."""
    out, names = [], {}
    for m in messages:
        role, c = m.get("role"), m.get("content") or ""
        if role == "user" and c.startswith("CONTEXT:"):
            ctx = parse_json_loose(c[len("CONTEXT:"):].strip())
            out.append({"role": "user", "content": render_context(ctx) if isinstance(ctx, dict) else c})
        elif role == "user" and c.startswith("VERIFIER_REJECTED"):
            fb = parse_json_loose(c.split("\n", 1)[1]) if "\n" in c else {}
            bad = [f"{x.get('rule')}: {x.get('detail')}" for x in (fb or {}).get("checks", []) if not x.get("passed")]
            out.append({"role": "user", "content": "VERIFIER REJECTED your conclusion. Failed checks: " + " | ".join(bad)
                        + ". " + ((fb or {}).get("instruction") or "Correct the cause, evidence and action; gather missing evidence with tools if needed. If unsupported, submit inconclusive.")})
        elif role == "tool":
            p = parse_json_loose(c) or {}
            out.append({"role": "tool", "tool_name": m.get("tool_name", ""), "content": render_result(m.get("tool_name", ""), p),
                        "_p": p})
        else:
            mm = dict(m)
            mm.pop("tool_call_id", None)
            if role == "assistant":
                mm["tool_calls"] = [{"function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or {}}}
                                    for tc in m.get("tool_calls") or []]
                if not mm["tool_calls"]:
                    mm.pop("tool_calls")
            out.append(mm)

    def total():
        return sum(est_tokens(str(m.get("content", "")) + json.dumps(m.get("tool_calls", ""))) for m in out)
    # collapse oldest tool results first, keep the most recent assistant turn's results intact
    last_asst = max((i for i, m in enumerate(out) if m["role"] == "assistant"), default=-1)
    for i, m in enumerate(out):
        if total() <= budget_tokens:
            break
        if m["role"] == "tool" and i < last_asst:
            m["content"] = render_result(m["tool_name"], m["_p"], full=False)
    for m in out:
        m.pop("_p", None)
    return out
