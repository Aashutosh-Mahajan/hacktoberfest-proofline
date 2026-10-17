"""Honest failover: explicit target, healthy-target selection, partner capacity/overload, warnings, partial recovery."""
import pytest

from mayday.core.bus import Bus
from mayday.sim.runtime import Simulation


def _sim(sc, seed=2, sev=1.0, adv=40_000):
    s = Simulation(Bus(), seed, sc, 15, sev, emit_metrics=False)
    s.advance(adv)
    return s


@pytest.mark.parametrize("sc", ["S2", "S3"])
def test_feasible_failover_to_healthiest_bank(sc):
    s = _sim(sc, sev=1.0)
    w = s.world
    pv = w.preview_action("failover_bank", {"bank": "BANK_X"})
    assert pv["target_bank"] == "BANK_Y" and pv["from_bank"] == "BANK_X"
    assert pv["recovers"] is True and pv["partial"] is False and pv["recovery_pct"] > 98
    hr = pv["target_headroom_pct"]
    assert 50 < hr["before"] < 70 and 0 < hr["after"] < 15
    assert any("BANK_Y would run at 9" in x for x in pv["warnings"])
    assert any("routing contract with BANK_Y" in x for x in pv["warnings"])
    assert any("hides the symptom" in x and "BANK_X" in x for x in pv["warnings"])
    assert pv["wall_ms"] < 300
    # apply with the explicit target and verify reality matches the twin
    at = w.telemetry.now()
    r = w.apply_action("failover_bank", {"bank": "BANK_X", "from_bank": "BANK_X", "to_bank": "BANK_Y"})
    assert r["ok"] and r["to_bank"] == "BANK_Y"
    s.advance(40_000)
    rec = w.measure_recovery(at)
    assert rec is not None and abs(rec - pv["expected_recovery_ms"]) <= 3000
    st = w.telemetry.bank_stats(w.telemetry.now() - 20_000, w.telemetry.now())
    assert st["BANK_X"]["total"] < 50 and st["BANK_Y"]["tps"] > 200      # Y really carries X's traffic


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_high_severity_failover_only_partially_recovers(seed):
    s = _sim("S2", seed=seed, sev=1.4)
    w = s.world
    pv = w.preview_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Y"})
    assert pv["recovers"] is False and pv["partial"] is True
    assert pv["improvement_pp"] > 10 and 70 < pv["recovery_pct"] < 95
    assert pv["target_headroom_pct"]["after"] < 0
    assert any("overloaded" in x and "BANK_Y" in x for x in pv["warnings"])
    assert any("partial" in x.lower() for x in pv["warnings"])
    # reality agrees: after applying, success stays below baseline, and BANK_Y shows rate limiting / timeouts
    w.apply_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Y"})
    s.advance(60_000)
    now = w.telemetry.now()
    y = w.telemetry.bank_stats(now - 20_000, now)["BANK_Y"]
    assert w.success_rate_now(20) < s.engine.ref_success - 3
    assert y["success_rate"] < 95 and y["p95_ms"] > 600
    codes = {c["code"] for c in w.telemetry.decline_breakdown(now - 20_000, now)["top_codes"]}
    assert "TD_RATE_LIMITED" in codes


def test_target_overload_depends_on_target_capacity():
    s = _sim("S2", sev=1.0)
    pz = s.world.preview_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Z"})
    py = s.world.preview_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Y"})
    assert pz["target_headroom_pct"]["after"] < py["target_headroom_pct"]["after"]


def test_no_healthy_target_s6_picks_z_and_none_left():
    s = _sim("S6", sev=1.0)
    w = s.world
    # BANK_Y is itself degraded in S6, so BANK_Z is the only healthy partner for BANK_X
    pv = w.preview_action("failover_bank", {"bank": "BANK_X"})
    assert pv["target_bank"] == "BANK_Z"
    bad = w.preview_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Y"})
    assert bad["feasible"] is False and bad["recovers"] is False and bad["warnings"]
    r = w.apply_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_Y"})
    assert r["ok"] is False and "healthy" in r["detail"]
    assert w.apply_action("failover_bank", {"bank": "BANK_X", "to_bank": "BANK_X"})["ok"] is False


def test_no_healthy_bank_to_receive_traffic():
    s = _sim("S2")
    w = s.world
    assert w.apply_action("failover_bank", {"bank": "BANK_Y", "to_bank": "BANK_Z"})["ok"]
    s.advance(40_000)
    # BANK_X is down and BANK_Y is already failed over: nothing healthy remains for BANK_Z's traffic
    pv = w.preview_action("failover_bank", {"bank": "BANK_Z"})
    assert pv["feasible"] is False and pv["recovers"] is False and pv["target_bank"] is None
    assert "no healthy bank to receive traffic" in pv["warnings"][0].lower()
    r = w.apply_action("failover_bank", {"bank": "BANK_Z"})
    assert r == {"ok": False, "detail": "no healthy bank to receive traffic"}


def test_validation_and_idempotence():
    s = _sim("S2")
    w = s.world
    assert w.apply_action("failover_bank", {"bank": "BANK_Q"})["ok"] is False
    assert w.apply_action("failover_bank", {"bank": "BANK_X", "to_bank": "NOPE"})["ok"] is False
    assert w.apply_action("failover_bank", {})["ok"] is False
    assert w.apply_action("failover_bank", {"bank": "BANK_X"})["to_bank"] == "BANK_Y"      # target auto-chosen
    assert w.apply_action("failover_bank", {"bank": "BANK_X"})["ok"]                       # idempotent


def test_other_previews_have_warnings_field_and_no_regression():
    s = _sim("S1")
    pv = s.world.preview_action("rollback_deploy", {"deploy_id": "482"})
    assert pv["recovers"] and pv["partial"] is False and isinstance(pv["warnings"], list) and pv["recovery_pct"] > 98
    assert "target_bank" not in pv
    n = s.world.preview_action("notify_only", {})
    assert not n["recovers"] and n["warnings"]
