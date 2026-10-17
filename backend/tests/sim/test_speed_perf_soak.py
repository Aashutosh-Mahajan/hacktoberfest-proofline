import gc
import os
import time

import pytest

from mayday.api.app import Hub
from mayday.core.bus import Bus
from mayday.sim.engine import Engine
from mayday.sim.runtime import Simulation


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1e6
    except ImportError:  # pragma: no cover
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                       [(n, ctypes.c_size_t) for n in ("Peak", "WorkingSetSize", "a", "b", "c", "d", "e", "f")]
        pmc = PMC(); pmc.cb = ctypes.sizeof(pmc)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1e6


# ---------------------------------------------------------------- speed control
def _live(speed, secs):
    bus = Bus()
    s = Simulation(bus, 1, "S0", emit_metrics=False)
    s.speed = speed
    t0 = s.engine.now
    s.start()
    time.sleep(secs)
    s.stop()
    return s, (s.engine.now - t0) / 1000.0


def test_speed_scales_sim_time():
    s1, d1 = _live(1.0, 2.2)
    s4, d4 = _live(4.0, 2.2)
    assert 1.0 <= d1 <= 3.5
    assert d4 > 2.5 * d1 and 5.0 <= d4 <= 14.0


def test_pause_resume_and_zero_speed():
    bus = Bus()
    s = Simulation(bus, 1, "S0", emit_metrics=False)
    s.speed = 5.0
    s.start()
    time.sleep(0.8)
    s.pause(True)
    time.sleep(0.15)
    t = s.engine.now
    time.sleep(0.8)
    assert s.engine.now == t and s.status()["running"] is False
    s.set_speed(0)
    s.pause(False)
    time.sleep(0.6)
    assert s.engine.now == t                      # speed 0: running but frozen
    s.set_speed(5.0)
    time.sleep(1.0)
    s.stop()
    assert s.engine.now > t


def test_speed_status_and_clamping_and_manual_advance():
    bus = Bus()
    s = Simulation(bus, 1, "S0", emit_metrics=False)
    s.set_speed(-3)
    assert s.speed == 0.0 and s.status()["speed"] == 0.0
    s.set_speed(8)
    ev = [e for e in bus.since(0) if e["type"] == "sim.status"]
    assert ev[-1]["data"]["speed"] == 8
    t = s.engine.now
    s.advance(10_000)
    assert s.engine.now - t == 10_000          # manual advance is exact and independent of speed/paused


def test_speed_is_stable_across_reset():
    bus = Bus()
    s = Simulation(bus, 1, "S1", emit_metrics=False)
    s.set_speed(3)
    s.reset("S2", 4)
    assert s.speed == 3 and s.engine.p.scenario == "S2"


# ---------------------------------------------------------------- perf (PRD NFR: >=300 txn/s, <15% of one core)
def test_realtime_cost_under_15pct_core():
    bus = Bus()
    s = Simulation(bus, 1, "S1")                       # full pipeline incl. metrics events + detector
    t0 = time.process_time()
    s.advance(120_000)
    cpu_per_sim_s = (time.process_time() - t0) / 120.0
    assert cpu_per_sim_s < 0.15, cpu_per_sim_s          # at 1x real time this is the core fraction used
    tps = sum(v["tps"] for v in s.world.telemetry.bank_stats(s.engine.now - 10_000, s.engine.now).values())
    assert tps >= 300


def test_determinism_with_runtime_pipeline():
    def run():
        bus = Bus(); s = Simulation(bus, 9, "S2", emit_metrics=False)
        s.advance(70_000)
        return s.engine.telemetry_hash(), [(e["type"], e["ts"]) for e in bus.since(0) if e["type"].startswith("incident.o")]
    assert run() == run()


# ---------------------------------------------------------------- soak
@pytest.mark.slow
def test_soak_60_sim_minutes_memory_and_ring_buffers_bounded(tmp_path):
    hub = Hub(autostart=False, use_agent=False, runs_dir=tmp_path / "runs")
    hub.start_scenario("S2", seed=3, fault_offset_s=2400)       # fault at minute 40, ring wraps at minute 50
    gc.collect()
    base = rss_mb()
    peak = base
    t0 = time.time()
    for minute in range(60):
        hub.sim.advance(60_000)
        if minute % 10 == 9:
            peak = max(peak, rss_mb())
    wall = time.time() - t0
    peak = max(peak, rss_mb())
    e = hub.sim.engine
    types = [x["type"] for x in hub.bus.since(0)]
    assert e.now - 1_760_000_000_000 >= 60 * 60_000 + 300_000
    # ring buffers capped
    assert len(hub.bus._ring) <= 5000
    assert len(hub.tracker.recent) <= 480 and len(hub.sim.recent) <= 480
    assert len(e.logs) <= 80_000 and len(e.log_ts) <= 80_000
    assert e.tick - e.lo <= e.cap
    assert hub.recorder._n <= 200_000 and len(hub.recorder._buf) <= 40_000
    # still correct after the ring wrapped: exactly one incident, closed after the 7-minute outage, no later false alarm
    assert peak < 1500, peak                           # PRD: backend <= 1.5 GB RSS
    assert peak - base < 600, (base, peak)
    print(f"soak: 60 sim-min in {wall:.1f}s wall, RSS base {base:.0f} MB peak {peak:.0f} MB")


@pytest.mark.slow
def test_soak_incident_lifecycle_across_ring_wrap():
    bus = Bus()
    s = Simulation(bus, 3, "S2", 2400, 1.0, emit_metrics=False)
    s.advance(60 * 60_000)
    ev = [e["type"] for e in bus.since(0) if e["type"] in ("incident.opened", "incident.closed")]
    assert ev == ["incident.opened", "incident.closed"]
    assert s.engine.lo > 0                              # buffer actually wrapped
