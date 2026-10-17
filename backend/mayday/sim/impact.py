"""Impact math (FR-27 blast radius, FR-28 rupees saved). Standalone, no engine deps."""
from __future__ import annotations

DEFAULT_MANUAL_MTTR_MS = 25 * 60 * 1000


def paise_at_risk_per_min(tps: float, failing_pct: float, avg_ticket_paise: int = 45000) -> float:
    """Paise/min of failing payment value. failing_pct is 0..100."""
    return max(0.0, float(tps)) * 60.0 * (max(0.0, float(failing_pct)) / 100.0) * avg_ticket_paise


def compute_blast(scope: str, tps: float, failing_pct: float, duration_s: float = 60.0,
                  avg_ticket_paise: int = 45000) -> dict:
    """Blast radius: scope, % failing, failed txn count over duration_s, paise/min at risk."""
    failing_pct = max(0.0, min(100.0, float(failing_pct)))
    failed = int(round(max(0.0, tps) * max(0.0, duration_s) * failing_pct / 100.0))
    return {
        "scope": scope,
        "failing_pct": round(failing_pct, 2),
        "failed_txn_count": failed,
        "paise_at_risk_per_min": round(paise_at_risk_per_min(tps, failing_pct, avg_ticket_paise), 2),
    }


def compute_savings(failure_rate_loss_paise_per_min: float, actual_duration_ms: int,
                    actual_loss_paise: int,
                    assumed_manual_mttr_ms: int = DEFAULT_MANUAL_MTTR_MS) -> dict:
    """Shape of WS event impact.savings (without incident_id). saved never negative."""
    counterfactual = int(round(max(0.0, failure_rate_loss_paise_per_min) * assumed_manual_mttr_ms / 60000.0))
    actual = int(max(0, actual_loss_paise))
    return {
        "actual_loss_paise": actual,
        "counterfactual_loss_paise": counterfactual,
        "saved_paise": max(0, counterfactual - actual),
        "assumed_manual_mttr_ms": int(assumed_manual_mttr_ms),
    }
