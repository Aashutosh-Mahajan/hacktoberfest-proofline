"""Pure aggregation: list of per-run result dicts -> EvalReport (contracts/types.ts) + markdown.

Run result dict keys:
  scenario: "S0".."S6"; verifier_enabled: bool
  incident: bool (an incident was raised)            -- S0 false-incident rate
  correct: bool (agent cause == ground truth cause)
  rca_s: float|None, tool_calls: int, verifier_rejections: int, conclusion_attempts: int
  twin_error_s: float|None
  remediated: bool (agent proposed/applied a remediation action)
  error: optional str
"""
from __future__ import annotations

import math
from datetime import date
from statistics import mean, median
from typing import Any, Iterable

ORDER = ["S0", "S1", "S2", "S3", "S5", "S4", "S6"]


def _pct(xs: list) -> float:
    return round(100.0 * sum(1 for x in xs if x) / len(xs), 1) if xs else 0.0


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _accuracy(runs: list[dict]) -> float:
    return round(sum(1 for r in runs if r.get("correct")) / len(runs), 4) if runs else 0.0


def build_row(scenario: str, runs: list[dict]) -> dict:
    rca = [r["rca_s"] for r in runs if r.get("rca_s") is not None]
    twin = [abs(r["twin_error_s"]) for r in runs if r.get("twin_error_s") is not None]
    attempts = sum(r.get("conclusion_attempts", 0) for r in runs)
    rej = sum(r.get("verifier_rejections", 0) for r in runs)
    row: dict[str, Any] = {
        "scenario": scenario,
        "runs": len(runs),
        "accuracy": _accuracy(runs),
        "median_rca_s": round(median(rca), 2) if rca else 0.0,
        "p90_rca_s": round(percentile(rca, 0.9), 2) if rca else 0.0,
        "mean_tool_calls": round(mean(r.get("tool_calls", 0) for r in runs), 2) if runs else 0.0,
        "verifier_rejection_rate": round(rej / attempts, 4) if attempts else 0.0,
        "twin_error_s": round(mean(twin), 2) if twin else 0.0,
    }
    if scenario == "S3":
        row["decoy_cleared_pct"] = _pct([r.get("correct") for r in runs])
    if scenario == "S5":
        row["correctly_not_remediated_pct"] = _pct([not r.get("remediated") for r in runs])
    return row


def build_report(results: Iterable[dict], model: str = "scripted", generated: str | None = None) -> dict:
    results = list(results)
    main = [r for r in results if r.get("verifier_enabled", True)]
    by: dict[str, list[dict]] = {}
    for r in main:
        by.setdefault(r["scenario"], []).append(r)
    s0 = by.get("S0", [])
    rows = [build_row(s, by[s]) for s in sorted(by, key=lambda s: ORDER.index(s) if s in ORDER else 99)]
    ablations = []
    scored = [r for r in main if r["scenario"] != "S0"]
    off = [r for r in results if not r.get("verifier_enabled", True) and r["scenario"] != "S0"]
    if scored:
        ablations.append({"name": "verifier on", "accuracy": _accuracy(scored), "runs": len(scored)})
    if off:
        ablations.append({"name": "verifier off", "accuracy": _accuracy(off), "runs": len(off)})
    return {
        "generated": generated or date.today().isoformat(),
        "model": model,
        "total_runs": len(results),
        "false_incident_rate": round(sum(1 for r in s0 if r.get("incident")) / len(s0), 4) if s0 else 0.0,
        "rows": rows,
        "ablations": ablations,
    }


def to_markdown(rep: dict) -> str:
    L = [f"# Proofline eval - {rep['generated']}", "",
         f"Model: `{rep['model']}` | total runs: {rep['total_runs']} | false-incident rate (S0): {rep['false_incident_rate']:.1%}", "",
         "| Scenario | Runs | Accuracy | Decoy cleared % | Not remediated % | Median RCA s | p90 RCA s | Tool calls | Verifier rej. | Twin err s |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        L.append(f"| {r['scenario']} | {r['runs']} | {r['accuracy']:.1%} | {r.get('decoy_cleared_pct', '-')} | "
                 f"{r.get('correctly_not_remediated_pct', '-')} | {r['median_rca_s']} | {r['p90_rca_s']} | "
                 f"{r['mean_tool_calls']} | {r['verifier_rejection_rate']:.1%} | {r['twin_error_s']} |")
    L += ["", "## Ablations", "", "| Config | Accuracy | Runs |", "|---|---|---|"]
    for a in rep["ablations"]:
        L.append(f"| {a['name']} | {a['accuracy']:.1%} | {a['runs']} |")
    return "\n".join(L) + "\n"
