import time

import pytest

from mayday.core.bus import Bus
from mayday.core.protocols import Telemetry, World
from mayday.sim.engine import create_world, Engine
from mayday.sim.runtime import Simulation


def test_protocols():
    w = create_world(1, "S1")
    assert isinstance(w, World) and isinstance(w.telemetry, Telemetry)


def test_determinism_and_batching_independence():
    a = Engine(7, "S1"); a.step(400)
    b = Engine(7, "S1"); [b.step(40) for _ in range(10)]
    assert a.telemetry_hash() == b.telemetry_hash()
    c = Engine(8, "S1"); c.step(400)
    assert c.telemetry_hash() != a.telemetry_hash()


def test_reset_restores_baseline():
    bus = Bus(); s = Simulation(bus, 3, "S1")
    h0 = Engine(3, "S0").telemetry_hash()
    s.advance(60_000)
    s.reset("S0")
    assert s.engine.telemetry_hash() == h0


def test_throughput_and_s1_dip():
    w = create_world(1, "S1", 15)
    base = w.success_rate_now(30)
    assert 97 < base < 100
    w.run_fast(60)
    assert w.success_rate_now(10) < base - 5
    st = w.telemetry.bank_stats(w.telemetry.now() - 10000, w.telemetry.now())
    assert st["BANK_X"]["timeout_pct"] > 15 and st["BANK_Y"]["timeout_pct"] == 0
    tps = sum(v["tps"] for v in st.values())
    assert tps >= 300


def test_notify_only_does_not_recover_and_rollback_does():
    w = create_world(1, "S1", 15); w.run_fast(45)
    t0 = time.perf_counter()
    p = w.preview_action("rollback_deploy", {"deploy_id": "deploy:482"})
    assert (time.perf_counter() - t0) < 0.3
    assert p["recovers"] and 0 < p["expected_recovery_ms"] < 15000
    n = w.preview_action("notify_only", {})
    assert not n["recovers"]
    assert not w.preview_action("enable_circuit_breaker", {"bank": "BANK_X"})["recovers"]
    assert w.preview_action("failover_bank", {"bank": "BANK_X"})["recovers"]


def test_fork_does_not_touch_live():
    w = create_world(2, "S1", 15); w.run_fast(30)
    h = w.telemetry_hash(); now = w.telemetry.now()
    f = w.fork(); f.run_fast(60); f.apply_action("rollback_deploy", {"deploy_id": "482"})
    assert w.telemetry_hash() == h and w.telemetry.now() == now and f.telemetry.now() > now


@pytest.mark.parametrize("delay", [0, 7])
def test_preview_matches_reality_within_3s(delay):
    w = create_world(5, "S1", 15); w.run_fast(40)
    p = w.preview_action("rollback_deploy", {"deploy_id": "482"})
    w.run_fast(delay)
    at = w.telemetry.now()
    w.apply_action("rollback_deploy", {"deploy_id": "482"})
    w.run_fast(40)
    actual = w.measure_recovery(at)
    assert actual is not None and abs(actual - p["expected_recovery_ms"]) <= 3000


def test_s0_no_false_incidents_30min():
    bus = Bus(); s = Simulation(bus, 11, "S0", emit_metrics=False)
    s.advance(30 * 60_000)
    assert [e for e in bus.since(0) if e["type"].startswith("incident")] == []


@pytest.mark.parametrize("sc,seed", [("S1", 1), ("S2", 2), ("S3", 3), ("S5", 4)])
def test_detector_opens_once(sc, seed):
    bus = Bus(); s = Simulation(bus, seed, sc, 15)
    s.advance(100_000)
    opened = [e for e in bus.since(0) if e["type"] == "incident.opened"]
    assert len(opened) == 1
    inc = opened[0]["data"]["incident"]
    gt = s.world.ground_truth()
    assert 0 <= inc["start_ts"] - gt["fault_start_ts"] <= 8000
    assert opened[0]["ts"] - gt["fault_start_ts"] <= 25000
    if sc == "S5":
        assert inc["decline_breakdown"]["business_pct"] > 70
    else:
        assert inc["decline_breakdown"]["technical_pct"] > 70


def test_s3_decoy_and_bulletin():
    w = create_world(3, "S3", 15)
    assert w.telemetry.read_bulletin("BANK_X") is None
    w.run_fast(60)
    d = {x["number"]: x for x in w.telemetry.deploys(0, w.telemetry.now() + 10**9)}
    assert 483 in d and d[483]["ts"] - w.ground_truth()["fault_start_ts"] > 30000
    b = w.telemetry.read_bulletin("BANK_X")
    assert b and b["png_path"].endswith(".png")
    tw = w.telemetry.bulletin_truth_window("BANK_X")
    assert tw and tw[0] >= w.ground_truth()["fault_start_ts"] - 5000


def test_s5_notify_only_nothing_broken():
    w = create_world(4, "S5", 15); w.run_fast(40)
    p = w.preview_action("notify_only", {})
    assert p["technical_healthy"] and p["recovers"]
