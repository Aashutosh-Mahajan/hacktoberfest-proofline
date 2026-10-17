"""Headless eval harness: python -m mayday.eval.run [--runs N] [--scenarios S0,S1,S3,S5] [--no-verifier] [--out DIR]

Wiring is discovered at runtime (sim factory, detector, orchestrator, gateway) so it adapts to the
other packages.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import random
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from .report import build_report, to_markdown

HORIZON_S = 300          # sim seconds to run waiting for a detection
STEP_S = 5
AGENT_TIMEOUT_S = 60.0
MAX_AGENT_STEPS = 120    # sim-step budget while the agent works (each STEP_S)
VERIFIER_FLAGS = ("verifier_enabled", "enable_verifier", "use_verifier")


class CaptureBus:
    def __init__(self):
        self.events: list[dict] = []
        self._seq = 0

    def emit(self, type: str, data: dict, ts: Optional[int] = None) -> int:
        self._seq += 1
        self.events.append({"seq": self._seq, "ts": ts, "type": type, "data": data, "wall": time.perf_counter()})
        return self._seq

    def of(self, t: str) -> list[dict]:
        return [e for e in self.events if e["type"] == t]


def _try(mod: str, names: list[str]) -> Optional[Callable]:
    try:
        m = importlib.import_module(mod)
    except Exception:
        return None
    for n in names:
        if hasattr(m, n):
            return getattr(m, n)
    return None


def find_world_factory() -> Optional[Callable]:
    for mod in ("mayday.sim.engine", "mayday.sim", "mayday.sim.world", "mayday.sim.factory"):
        f = _try(mod, ["create_world", "make_world", "new_world", "build_world"])
        if f:
            return f
    return None


def make_world(factory: Callable, seed: int, scenario: str, fault_offset_s: float, severity: float):
    sig = inspect.signature(factory)
    cand = {"seed": seed, "scenario": scenario, "fault_offset_s": fault_offset_s, "severity": severity}
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return factory(**cand)
    return factory(**{k: v for k, v in cand.items() if k in sig.parameters})


class _NoVerifier:
    """Ablation shim: used when Orchestrator has no verifier flag. Replaces orch.verifier with a pass-through."""

    def __init__(self, real):
        self._real = real
        self.catches = 0

    def verify(self, concl, incident):
        from mayday.agent.verifier import VerifyResult
        return VerifyResult(checks=[])

    def __getattr__(self, n):
        return getattr(self._real, n)


def make_orchestrator(world, bus, verifier_enabled: bool, wrong_first: bool = True):
    Orch = _try("mayday.agent.orchestrator", ["Orchestrator"])
    Gw = _try("mayday.agent.scripted", ["ScriptedGateway"]) or _try("mayday.agent", ["ScriptedGateway"])
    if Orch is None or Gw is None:
        raise RuntimeError("agent package not ready (Orchestrator/ScriptedGateway missing)")
    params = inspect.signature(Orch.__init__).parameters
    kw: dict[str, Any] = {"poll_s": 0.0} if "poll_s" in params else {}
    native = next((n for n in VERIFIER_FLAGS if n in params), None)
    if native:
        kw[native] = verifier_enabled
    gw = Gw(wrong_first=wrong_first) if "wrong_first" in inspect.signature(Gw.__init__).parameters else Gw()
    orch = Orch(world, bus, gw, **kw)
    if not verifier_enabled and not native:
        orch.verifier = _NoVerifier(orch.verifier)
    return orch


def find_detector(world):
    """Return a callable giving an incident dict (or None) per call. Uses mayday.sim.detector.Detector."""
    from mayday.sim.detector import Detector
    det = Detector(world.engine, world.telemetry)

    def poll():
        for typ, data, _ts in det.observe():
            if typ == "incident.opened":
                return data["incident"]
        return None
    return poll


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def args_timeout() -> float:
    return AGENT_TIMEOUT_S


def _norm(c: Any) -> str:
    return str(c or "").strip().lower()


async def run_one(factory, scenario: str, seed: int, verifier_enabled: bool, rng: random.Random,
                  wrong_first: bool = True) -> dict:
    fault_offset = rng.uniform(20, 60)
    severity = rng.uniform(0.6, 1.0)
    res: dict[str, Any] = dict(scenario=scenario, seed=seed, verifier_enabled=verifier_enabled, incident=False,
                               correct=False, rca_s=None, tool_calls=0, verifier_rejections=0,
                               conclusion_attempts=0, twin_error_s=None, remediated=False)
    try:
        world = make_world(factory, seed, scenario, fault_offset, severity)
        bus = CaptureBus()
        orch = make_orchestrator(world, bus, verifier_enabled, wrong_first)
        detect = find_detector(world)
        incident = None
        for _ in range(int(HORIZON_S / STEP_S)):
            world.run_fast(STEP_S)
            incident = detect()
            if incident:
                break
        gt = world.ground_truth() or {}
        if not incident:
            res["correct"] = scenario == "S0"
            return res
        res["incident"] = True
        if scenario == "S0":
            return res
        detected_ms = incident.get("detected_ts") if isinstance(incident, dict) else None
        t0_wall = time.perf_counter()
        orch.on_incident(incident)
        approved = False
        sim_budget = int(MAX_AGENT_STEPS)
        for _ in range(sim_budget * 20):
            prop = bus.of("action.proposed")
            if prop and not approved:
                p = prop[-1]["data"]
                approved = True
                res["remediated"] = p["action"]["kind"] != "notify_only"
                orch.approve(p["incident_id"], p["action"]["id"], p["approval_token"])
            if hasattr(orch, "join") and orch.join(0.0 if False else 0.01):
                break
            world.run_fast(1)
            if time.perf_counter() - t0_wall > args_timeout():
                res["error"] = "agent timeout"
                break
        rcas = bus.of("rca.ready")
        if rcas:
            rca = rcas[-1]["data"]["rca"]
            res["correct"] = _norm(rca.get("cause")) == _norm(gt.get("cause"))
            ts = rcas[-1].get("ts")
            if rca.get("inconclusive"):
                pass
            elif ts is not None and detected_ms is not None:
                res["rca_s"] = max(0.0, (ts - detected_ms) / 1000.0)
            else:
                res["rca_s"] = rcas[-1]["wall"] - t0_wall
        res["tool_calls"] = len(bus.of("agent.tool_call"))
        vr = bus.of("verify.result")
        res["conclusion_attempts"] = len(vr)
        res["verifier_rejections"] = sum(1 for e in vr if not e["data"].get("passed"))
        ex = bus.of("action.executed")
        if ex:
            d = ex[-1]["data"]
            if d.get("predicted_recovery_ms") is not None and d.get("actual_recovery_ms") is not None:
                res["twin_error_s"] = (d["predicted_recovery_ms"] - d["actual_recovery_ms"]) / 1000.0
        res["cause"] = (rcas[-1]["data"]["rca"] if rcas else {}).get("cause")
        res["truth"] = gt.get("cause")
    except Exception as e:  # never crash the harness
        res["error"] = f"{type(e).__name__}: {e}"
    return res


async def main_async(args) -> dict:
    factory = find_world_factory()
    if factory is None:
        raise SystemExit("sim not ready: no create_world factory found in mayday.sim")
    scenarios = [s for s in args.scenarios.split(",") if s]
    rng = random.Random(args.seed)
    if args.no_verifier:
        configs = [False]
    elif args.no_ablation:
        configs = [True]
    else:
        configs = [True, False]
    results = []
    for ver in configs:
        for sc in scenarios:
            if not ver and sc == "S0":
                continue
            for i in range(args.runs):
                results.append(await run_one(factory, sc, args.seed * 1000 + i, ver, rng, not args.no_wrong_first))
    errs = [r for r in results if r.get("error")]
    rep = build_report(results, model=args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (out / "raw.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    md = to_markdown(rep)
    if errs:
        md += f"\n{len(errs)} run(s) errored; first: `{errs[0]['error']}`\n"
    (out / f"{date.today().isoformat()}.md").write_text(md, encoding="utf-8")
    print(md)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--scenarios", default="S0,S1,S3,S5")
    ap.add_argument("--no-verifier", action="store_true", help="run ONLY with the verifier disabled")
    ap.add_argument("--no-ablation", action="store_true", help="skip the verifier-off ablation pass")
    ap.add_argument("--no-wrong-first", action="store_true", help="ScriptedGateway answers correctly first time (ablation becomes vacuous)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--model", default="scripted")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[3] / "eval" / "results"))
    args = ap.parse_args(argv)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
