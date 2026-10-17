import numpy as np
from PIL import Image
from mayday.sim.impact import compute_savings, compute_blast, paise_at_risk_per_min
from mayday.sim.bulletin import render_bulletin, extract_fields_from_meta
from mayday.sim.hashing import telemetry_hash


def test_savings():
    r = compute_savings(100000.0, 120000, 200000)
    assert set(r) == {"actual_loss_paise", "counterfactual_loss_paise", "saved_paise", "assumed_manual_mttr_ms"}
    assert r["counterfactual_loss_paise"] == 2_500_000 and r["saved_paise"] == 2_300_000
    assert compute_savings(1.0, 1, 10**9)["saved_paise"] == 0


def test_blast():
    assert paise_at_risk_per_min(10, 50) == 10 * 60 * 0.5 * 45000
    b = compute_blast("BANK_X", 10, 50, 60)
    assert b["failed_txn_count"] == 300 and b["failing_pct"] == 50


def test_bulletin(tmp_path):
    p = render_bulletin("BANK_X", "Degradation", "degraded", 1000, 2000, tmp_path / "b.png")
    assert Image.open(p).size == (900, 500)
    m = {"bank": "BANK_X", "status": "degraded", "claimed_start_ts": 1000, "claimed_end_ts": 2000}
    assert extract_fields_from_meta(m) == m


def test_hash():
    a = {"x": np.arange(5.0), "y": np.ones((2, 2), dtype=int)}
    assert telemetry_hash(a) == telemetry_hash(dict(reversed(list(a.items()))))
    b = {"x": np.arange(5.0) + 1e-9, "y": a["y"]}
    assert telemetry_hash(a) != telemetry_hash(b)
