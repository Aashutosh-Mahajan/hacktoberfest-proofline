"""Postmortem: deterministic timeline + narrative (model if available, scripted template otherwise), <=300 words."""
from __future__ import annotations

from typing import Optional


def _clock(ts: int) -> str:
    s = int(ts // 1000) % 86400
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def template_narrative(rca: dict, incident: dict, savings: Optional[dict], executed: Optional[dict]) -> str:
    blast = (incident or {}).get("blast", {})
    parts = [f"Root cause: {rca.get('cause_label')}. {rca.get('summary', '')}",
             f"Impact: {blast.get('scope', 'payments')}, ~{blast.get('failing_pct', 0):.0f}% of payments failing."]
    if executed and executed.get("recovered"):
        pr, ac = executed.get("predicted_recovery_ms"), executed.get("actual_recovery_ms")
        parts.append(f"Resolution: approved action restored health; twin predicted {pr // 1000 if pr else '?'}s, actual {ac // 1000 if ac else '?'}s.")
    elif executed:
        parts.append("Resolution: action applied but recovery was not confirmed; keep monitoring.")
    else:
        parts.append("Resolution: no infrastructure change was made.")
    if savings:
        parts.append(f"Loss avoided vs a {savings['assumed_manual_mttr_ms'] // 60000}-minute manual response: ₹{savings['saved_paise'] / 100:,.0f} (assumption labelled).")
    parts.append("Action items: add alerting on the failing scope; review change-safety checks; confirm stakeholder comms.")
    return " ".join(parts)


def build_postmortem(rca: dict, incident: dict, timeline: list, savings: Optional[dict], executed: Optional[dict],
                     gateway=None) -> str:
    narrative = None
    if gateway is not None and getattr(gateway, "backend", "") == "ollama":
        try:
            facts = template_narrative(rca, incident, savings, executed)
            r = gateway.chat([{"role": "system", "content": "Write a blameless incident postmortem narrative, max 250 words, plain text."},
                              {"role": "user", "content": facts}], None, timeout=30.0)
            narrative = r.text or None
        except Exception:
            narrative = None
    narrative = narrative or template_narrative(rca, incident, savings, executed)
    words = narrative.split()
    if len(words) > 300:
        narrative = " ".join(words[:300])
    lines = [f"# Postmortem — {rca.get('cause_label')}", "", "## Timeline"]
    for ts, text in sorted(timeline):
        lines.append(f"- `{_clock(ts)}` {text}")
    lines += ["", "## Narrative", narrative]
    return "\n".join(lines)
