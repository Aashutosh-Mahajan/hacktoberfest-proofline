"""Telemetry + control tools. Compact outputs; evidence registered in the EvidenceStore."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .evidence import EvidenceStore
from .util import on_path, trunc

BANKS = ("BANK_X", "BANK_Y", "BANK_Z")
TELEMETRY_TOOLS = ("success_by", "decline_breakdown", "query_logs", "list_deploys", "get_deploy",
                   "traffic", "bank_stats", "read_bulletin", "preview_fix")
CONTROL_TOOLS = ("update_hypothesis", "submit_conclusion")
READ_ONLY_TOOLS = tuple(t for t in TELEMETRY_TOOLS if t != "preview_fix")


def _spec(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


_W = {"type": "string", "description": "incident | baseline | recent | integer seconds lookback"}
TOOL_SPECS = [
    _spec("success_by", "Success-rate trend grouped by bank/service/code.",
          {"by": {"type": "string", "enum": ["bank", "service", "code"]}, "window": _W,
           "technical_only": {"type": "boolean"}}, ["by"]),
    _spec("decline_breakdown", "Technical vs business decline split, top codes, optional grouping.",
          {"window": _W, "by": {"type": "string", "enum": ["bank", "code", "service"]}}),
    _spec("query_logs", "Search sampled failure logs.",
          {"service": {"type": "string"}, "bank": {"type": "string"}, "code": {"type": "string"},
           "window": _W, "limit": {"type": "integer"}}),
    _spec("list_deploys", "Recent deploy/config events with timing relative to the dip.", {"window": _W}),
    _spec("get_deploy", "Full deploy detail including diff.", {"deploy_id": {"type": "string"}}, ["deploy_id"]),
    _spec("traffic", "Traffic volume vs baseline.", {"window": _W}),
    _spec("bank_stats", "Per-bank success, technical success, p95, tps, 5xx vs baseline.", {"window": _W}),
    _spec("read_bulletin", "Read a bank status bulletin as structured data.",
          {"bank": {"type": "string", "enum": list(BANKS)}}, ["bank"]),
    _spec("preview_fix", "Run a fix in the digital twin and see whether it recovers.",
          {"kind": {"type": "string"}, "params": {"type": "object"}}, ["kind"]),
    _spec("update_hypothesis", "Set a hypothesis status with a one-line reason (<=120 chars).",
          {"id": {"type": "string"}, "status": {"type": "string", "enum": ["open", "supported", "ruled_out"]},
           "reason": {"type": "string"}}, ["id", "status", "reason"]),
    _spec("submit_conclusion", "Submit the final root-cause conclusion for deterministic verification.",
          {"cause": {"type": "string", "description": "deploy:<n> | bank:<BANK> | business:<code> | traffic_spike | inconclusive"},
           "confidence": {"type": "number"}, "evidence_ids": {"type": "array", "items": {"type": "string"}},
           "ruled_out": {"type": "array", "items": {"type": "object"}},
           "proposed_action": {"type": "object", "description": "{kind, params, rationale}"},
           "summary": {"type": "string"}},
          ["cause", "confidence", "evidence_ids", "ruled_out", "summary"]),
]


def specs(names) -> list:
    return [s for s in TOOL_SPECS if s["function"]["name"] in names]


def _slim(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


_S, _I = {"type": "string"}, {"type": "integer"}
# Tiny schemas for small local models (every token counts on CPU). Only the tools Gemma needs; the rest stay callable.
SLIM_SPECS = {
    "decline_breakdown": _slim("decline_breakdown", "technical vs business declines", {"by": {"type": "string", "enum": ["bank", "code"]}}),
    "bank_stats": _slim("bank_stats", "per-bank success vs baseline", {}),
    "list_deploys": _slim("list_deploys", "recent deploys vs dip timing", {}),
    "traffic": _slim("traffic", "traffic vs baseline", {}),
    "query_logs": _slim("query_logs", "failure logs", {"bank": _S, "code": _S}),
    "read_bulletin": _slim("read_bulletin", "read bank status bulletin image", {"bank": _S}, ["bank"]),
    "get_deploy": _slim("get_deploy", "deploy diff", {"deploy_id": _S}, ["deploy_id"]),
    "submit_conclusion": _slim("submit_conclusion", "final root cause for verification", {
        "cause": {"type": "string", "description": "deploy:<n>|bank:<BANK>|business:<code>|traffic_spike|inconclusive"},
        "confidence": {"type": "number"}, "evidence_ids": {"type": "array", "items": _S},
        "ruled_out": {"type": "array", "items": {"type": "object", "properties": {"target": _S, "reason": _S}}},
        "action": {"type": "string", "description": "rollback_deploy|failover_bank|notify_only|enable_circuit_breaker|scale_gateway"},
        "summary": _S}, ["cause", "confidence", "evidence_ids", "ruled_out", "action", "summary"]),
}


def slim_specs(names) -> list:
    return [SLIM_SPECS[n] for n in SLIM_SPECS if n in names]


@dataclass
class ToolResult:
    ok: bool
    data: dict
    summary: str
    evidence_ids: list = field(default_factory=list)
    ms: int = 0


class Toolbox:
    def __init__(self, world, store: EvidenceStore, get_incident: Callable[[], Optional[dict]], gateway=None):
        self.world = world
        self.tel = world.telemetry
        self.store = store
        self.get_incident = get_incident
        self.gateway = gateway

    def _start(self) -> int:
        inc = self.get_incident()
        return int(inc["start_ts"]) if inc else self.tel.now() - 5 * 60_000

    def window(self, w: Any) -> tuple:
        now = self.tel.now()
        s = self._start()
        if w in (None, "", "incident"):
            return (s, max(now, s + 1000))
        if w == "baseline":
            return (s - 300_000, s - 15_000)
        if w == "recent":
            return (now - 30 * 60_000, now)
        try:
            return (now - int(float(w)) * 1000, now)
        except Exception:
            return (s, now)

    def call(self, name: str, args: dict) -> ToolResult:
        t0 = time.monotonic()
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in TELEMETRY_TOOLS:
            return ToolResult(False, {"error": f"unknown tool {name}"}, f"unknown tool {name}")
        try:
            data, summary, ev = fn(**(args or {}))
            r = ToolResult(True, data, summary, ev)
        except TypeError as e:
            r = ToolResult(False, {"error": f"bad arguments: {e}"}, f"bad arguments for {name}")
        except Exception as e:  # a tool failure must never crash the run
            r = ToolResult(False, {"error": str(e)[:200]}, trunc(f"{name} failed: {e}", 140))
        r.ms = int((time.monotonic() - t0) * 1000)
        return r

    # ---- tools ----
    def t_bank_stats(self, window="incident"):
        s, e = self.window(window)
        cur = self.tel.bank_stats(s, e)
        base = self.tel.bank_stats(self._start() - 300_000, self._start() - 15_000)
        out, ev, worst, worst_d = {}, [], None, -1.0
        for b, c in cur.items():
            bs = base.get(b, {})
            d = round((c.get("technical_success_rate", 0) - bs.get("technical_success_rate", 0)) * 100, 1)
            out[b] = {"tsr": round(c.get("technical_success_rate", 0), 4),
                      "base_tsr": round(bs.get("technical_success_rate", 0), 4), "d_pp": d,
                      "sr": round(c.get("success_rate", 0), 4), "p95": c.get("p95_ms"),
                      "base_p95": bs.get("p95_ms"), "tps": c.get("tps"), "err5xx_pct": c.get("error_5xx_pct")}
            if d < worst_d:
                worst, worst_d = b, d
            ev.append(self.store.register(f"ev_bank_{b}", "metric", f"{b} stats (incident vs baseline)", out[b], ts=e))
        s_ = f"worst bank {worst}: technical success {worst_d:+.1f}pp" if worst else "all banks within baseline"
        return {"banks": out, "worst": worst}, s_, ev

    def t_decline_breakdown(self, window="incident", by=None):
        s, e = self.window(window)
        r = self.tel.decline_breakdown(s, e, by)
        data = {"technical_pct": r.get("technical_pct"), "business_pct": r.get("business_pct"),
                "top_codes": (r.get("top_codes") or [])[:4]}
        if r.get("by"):
            data["by"] = r["by"]
        eid = self.store.register(f"ev_breakdown_{by or 'all'}", "breakdown",
                                  f"Decline breakdown{' by ' + by if by else ''}", data, ts=e)
        top = data["top_codes"][0] if data["top_codes"] else {}
        return data, (f"{data['technical_pct']:.0f}% technical / {data['business_pct']:.0f}% business; "
                      f"top {top.get('code')} {top.get('pct')}%"), [eid]

    def t_success_by(self, by="bank", window="incident", technical_only=False):
        s, e = self.window(window)
        r = self.tel.success_series(s, e, by, 5000, bool(technical_only))["series"]
        out = {}
        for k, pts in r.items():
            if pts:
                vals = [p[1] for p in pts]
                out[k] = {"first": round(vals[0], 4), "last": round(vals[-1], 4), "min": round(min(vals), 4)}
        eid = self.store.register(f"ev_metric_success_{by}", "metric", f"Success rate by {by}", out, ts=e)
        return {"series": out}, trunc(f"success by {by}: " + ", ".join(f"{k} {v['last']:.2f}" for k, v in out.items()), 120), [eid]

    def t_query_logs(self, service=None, bank=None, code=None, window="incident", limit=6):
        s, e = self.window(window)
        r = self.tel.query_logs(s, e, service, bank, code, min(int(limit or 6), 10))
        lines = [{"ts": l.get("ts"), "svc": l.get("service"), "bank": l.get("bank"), "code": l.get("code"),
                  "msg": trunc(l.get("msg", ""), 90)} for l in r.get("lines", [])][:6]
        key = re.sub(r"\W", "", f"{service or ''}{bank or ''}{code or ''}")
        eid = self.store.register(f"ev_log_{key or 'all'}", "log", f"Logs {bank or service or ''} {code or ''}".strip(),
                                  {"lines": lines, "total": r.get("total", 0)}, ts=e)
        return {"lines": lines, "total": r.get("total", 0)}, f"{r.get('total', 0)} matching log lines", [eid]

    def _deploy_row(self, d, start):
        t = round((d["ts"] - start) / 1000)
        return {"id": d["id"], "number": d["number"], "service": d["service"], "t_vs_dip_s": t,
                "before_dip": t <= 15, "on_path": on_path(d["service"]), "summary": trunc(d.get("summary", ""), 80),
                "banks": sorted(set(re.findall(r"BANK_[XYZ]", d.get("diff", "") + d.get("summary", ""))))}

    def _register_deploy(self, d, start):
        body = dict(d)
        body.update(self._deploy_row(d, start))
        return self.store.register(f"ev_deploy_{d['number']}", "deploy", f"Deploy #{d['number']} · {d['service']}",
                                   body, ts=d["ts"])

    def t_list_deploys(self, window="recent"):
        s, e = self.window(window)
        start = self._start()
        out, ev = [], []
        for d in sorted(self.tel.deploys(s, e), key=lambda d: -d["ts"])[:6]:
            out.append(self._deploy_row(d, start))
            ev.append(self._register_deploy(d, start))
        return {"deploys": out}, f"{len(out)} deploys in window", ev

    def t_get_deploy(self, deploy_id):
        m = re.search(r"(\d+)", str(deploy_id))
        now = self.tel.now()
        d = None
        for x in self.tel.deploys(now - 6 * 3600_000, now + 1):
            if x["id"] == str(deploy_id) or (m and str(x["number"]) == m.group(1)):
                d = x
                break
        d = d or self.tel.get_deploy(str(deploy_id))
        if not d:
            raise ValueError(f"deploy {deploy_id} not found")
        row = self._deploy_row(d, self._start())
        row["diff"] = trunc(d.get("diff", ""), 400)
        row["mentions_banks"] = sorted(set(re.findall(r"BANK_[XYZ]", d.get("diff", "") + d.get("summary", ""))))
        eid = self._register_deploy(d, self._start())
        return row, f"deploy #{d['number']} {d['service']} ({row['t_vs_dip_s']:+d}s vs dip)", [eid]

    def t_traffic(self, window="incident"):
        s, e = self.window(window)
        r = self.tel.traffic(s, e)
        data = {"baseline_tps": r.get("baseline_tps"), "ratio": round(r.get("ratio", 1.0), 2)}
        eid = self.store.register("ev_metric_traffic", "metric", "Traffic vs baseline", data, ts=e)
        return data, f"traffic {data['ratio']}x baseline", [eid]

    def t_read_bulletin(self, bank):
        b = str(bank or "").strip().upper().replace(" ", "_")
        bank = b if b.startswith("BANK_") else ("BANK_" + b if b in ("X", "Y", "Z") else b)
        info = self.tel.read_bulletin(bank)
        if not info:
            return {"available": False}, f"no bulletin for {bank}", []
        ex, note = None, "from bulletin metadata"
        if self.gateway is not None:
            try:
                if hasattr(self.gateway, "read_bulletin"):
                    ex, note = self.gateway.read_bulletin(info, bank, self.tel.now())
                else:
                    ex = self.gateway.extract_bulletin(info, bank)
            except Exception as e:
                ex, note = None, f"model read failed ({trunc(e, 60)}); used bulletin metadata"
        ex = ex or info.get("extracted")
        if not ex:
            raise ValueError("bulletin unreadable")
        from .verifier import bulletin_consistency
        ok, detail = bulletin_consistency(ex, self.tel.bulletin_truth_window(bank), self.tel.now())
        detail = f"{detail} [{note}]"
        b = {"image_url": info.get("image_url", ""), "extracted": ex, "consistent": ok, "detail": detail}
        eid = self.store.register(f"ev_bulletin_{bank}", "bulletin", f"{bank} status bulletin", {"bulletin": b},
                                  ts=ex.get("claimed_start_ts"))
        return ({"available": True, "extracted": ex, "consistent": ok, "detail": detail},
                trunc(f"bulletin {bank}: {ex.get('status')} · {detail}", 140), [eid])

    def t_preview_fix(self, kind, params=None):
        r = self.world.preview_action(kind, params or {}, 120)
        return ({"recovers": r["recovers"], "expected_recovery_ms": r["expected_recovery_ms"]},
                f"twin: {kind} {'recovers' if r['recovers'] else 'does NOT recover'} (~{r['expected_recovery_ms'] // 1000}s)", [])
