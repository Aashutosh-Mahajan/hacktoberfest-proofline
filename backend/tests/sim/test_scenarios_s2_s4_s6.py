"""S2 (bank outage + bulletin), S4 (traffic spike) and S6 (compound) produce a detectable incident with the right
ground truth, evidence in telemetry, and a fix that really recovers."""
import pytest

from mayday.core.bus import Bus
from mayday.sim.engine import create_world
from mayday.sim.runtime import Simulation


def _sim(sc, seed=1, off=15, adv=40_000):
    bus = Bus()
    s = Simulation(bus, seed, sc, off, 1.0, emit_metrics=False)
    s.advance(adv)
    return bus, s


def _opened(bus):
    return [e["data"]["incident"] for e in bus.since(0) if e["type"] == "incident.opened"]


# ---------------------------------------------------------------- S2
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_s2_incident_truth_and_fix(seed):
    bus, s = _sim("S2", seed)
    w = s.world
    inc = _opened(bus)
    assert len(inc) == 1 and inc[0]["decline_breakdown"]["technical_pct"] > 70
    assert w.ground_truth()["cause"] == "bank:BANK_X"
    assert "BANK_X" in inc[0]["blast"]["scope"]
    st = w.telemetry.bank_stats(w.telemetry.now() - 15_000, w.telemetry.now())
    assert st["BANK_X"]["error_5xx_pct"] > 8 and st["BANK_Y"]["error_5xx_pct"] < 2   # outage is bank-side only
    assert all(d["service"] != "bank-adapter" for d in w.telemetry.deploys(w.ground_truth()["fault_start_ts"] - 120_000, w.telemetry.now()))
    # fix: fail BANK_X over; rollback / notify do nothing
    assert w.preview_action("failover_bank", {"bank": "BANK_X"})["recovers"]
    assert not w.preview_action("rollback_deploy", {"deploy_id": "481"})["recovers"]
    pv = w.preview_action("failover_bank", {"bank": "BANK_X"})
    at = w.telemetry.now()
    w.apply_action("failover_bank", {"bank": "BANK_X"})
    s.advance(40_000)
    rec = w.measure_recovery(at)
    assert rec is not None and abs(rec - pv["expected_recovery_ms"]) <= 3000


def test_s2_bulletin_through_telemetry_and_http_path():
    bus, s = _sim("S2", 2, adv=20_000)
    tel = s.world.telemetry
    assert tel.read_bulletin("BANK_X") is None           # not yet published
    s.advance(30_000)
    b = tel.read_bulletin("BANK_X")
    assert b["image_url"] == "/api/bulletin/BANK_X.png" and b["png_path"].endswith(".png")
    assert "S2" not in b["png_path"]                      # file name must not leak the scenario id (chaos)
    assert set(b["extracted"]) == {"bank", "status", "claimed_start_ts", "claimed_end_ts"}
    assert tel.read_bulletin("BANK_Y") is None


def test_s2_bulletin_wrong_in_about_35pct_of_seeds_and_truth_check():
    wrong = 0
    n = 80
    for seed in range(1, n + 1):
        w = create_world(seed, "S2", 15)
        spec = w.engine.bulletins["BANK_X"]
        wrong += spec["wrong"]
        f0 = w.engine.f0
        if spec["wrong"]:
            assert spec["claimed_end_ts"] < f0 + 30_000 and spec["status"] == "operational"   # contradicts the outage
        else:
            assert spec["claimed_start_ts"] <= f0 and spec["claimed_end_ts"] >= f0 + 400_000 and spec["status"] == "degraded"
    assert 0.2 <= wrong / n <= 0.5, wrong / n


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_s2_bulletin_vs_telemetry_consistency(seed):
    """An honest bulletin overlaps the telemetry-derived failure window; a wrong one does not."""
    bus, s = _sim("S2", seed, adv=75_000)
    tel = s.world.telemetry
    b = tel.read_bulletin("BANK_X")
    tw = tel.bulletin_truth_window("BANK_X")
    assert b and tw
    ex = b["extracted"]
    overlap = min(ex["claimed_end_ts"], tw[1]) - max(ex["claimed_start_ts"], tw[0])
    wrong = s.engine.bulletins["BANK_X"]["wrong"]
    assert (overlap <= 0) == wrong


# ---------------------------------------------------------------- S4
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_s4_traffic_spike(seed):
    bus, s = _sim("S4", seed)
    w = s.world
    inc = _opened(bus)
    assert len(inc) == 1
    gt = w.ground_truth()
    assert gt["cause"] == "traffic_spike" and 0 <= inc[0]["start_ts"] - gt["fault_start_ts"] <= 6000
    tr = w.telemetry.traffic(w.telemetry.now() - 15_000, w.telemetry.now())
    assert tr["ratio"] > 1.8                              # ~3x load
    bd = w.telemetry.decline_breakdown(w.telemetry.now() - 15_000, w.telemetry.now())
    assert bd["top_codes"][0]["code"] == "TD_RATE_LIMITED"
    assert not w.preview_action("rollback_deploy", {"deploy_id": "481"})["recovers"]
    pv = w.preview_action("scale_gateway", {})
    assert pv["recovers"] and 0 < pv["expected_recovery_ms"] <= 15_000
    at = w.telemetry.now()
    w.apply_action("scale_gateway", {})
    s.advance(40_000)
    rec = w.measure_recovery(at)
    assert rec is not None and abs(rec - pv["expected_recovery_ms"]) <= 3000


# ---------------------------------------------------------------- S6
@pytest.mark.parametrize("seed", list(range(1, 13)))
def test_s6_compound(seed):
    bus, s = _sim("S6", seed)
    w = s.world
    inc = _opened(bus)
    assert len(inc) == 1
    gt = w.ground_truth()
    assert gt["causes"] == ["deploy:482", "bank:BANK_Y"]
    assert w.telemetry.get_deploy("482")["service"] == "payment-orchestrator"
    st = w.telemetry.bank_stats(w.telemetry.now() - 20_000, w.telemetry.now())
    assert st["BANK_X"]["timeout_pct"] > 15                       # cause 1: deploy -> BANK_X timeouts
    assert st["BANK_Y"]["error_5xx_pct"] > 1.0 and st["BANK_Y"]["timeout_pct"] < 1      # cause 2: BANK_Y 5xx, separate signature
    assert st["BANK_Z"]["error_5xx_pct"] < 0.8
    pv = w.preview_action("rollback_deploy", {"deploy_id": "482"})
    assert pv["recovers"] and pv["expected_recovery_ms"] <= 15_000   # rollback fixes the headline incident deterministically
    at = w.telemetry.now()
    w.apply_action("rollback_deploy", {"deploy_id": "482"})
    s.advance(40_000)
    rec = w.measure_recovery(at)
    assert rec is not None and rec <= 15_000
    # residual BANK_Y degradation remains until it is failed over too
    st2 = w.telemetry.bank_stats(w.telemetry.now() - 20_000, w.telemetry.now())
    assert st2["BANK_X"]["timeout_pct"] < 1 and st2["BANK_Y"]["error_5xx_pct"] > 1.0
    w.apply_action("failover_bank", {"bank": "BANK_Y"})
    s.advance(30_000)
    assert w.success_rate_now(10) > s.engine.ref_success - 1.0
