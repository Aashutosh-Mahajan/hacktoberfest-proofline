"""Interfaces between the simulator/telemetry layer (owner: sim) and the agent layer (owner: agent).

The sim package implements `Telemetry` and `World`. The agent package depends ONLY on these
protocols (plus `EventBus`), so both can be built and tested in parallel. All times are simulated
epoch milliseconds. Money is integer paise.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

BANKS = ("BANK_X", "BANK_Y", "BANK_Z")
TECH_CODES = ("TD_TIMEOUT", "TD_BANK_5XX", "TD_RATE_LIMITED")
BIZ_CODES = ("BD_WRONG_PIN", "BD_LIMIT_EXCEEDED", "BD_INSUFFICIENT_FUNDS")


@runtime_checkable
class EventBus(Protocol):
    def emit(self, type: str, data: dict, ts: Optional[int] = None) -> int:
        """Publish an Envelope-typed event (see contracts/types.ts). Returns its seq."""


@runtime_checkable
class Telemetry(Protocol):
    """Read-only view over the simulated system. Every method returns plain JSON-able dicts."""

    def now(self) -> int: ...

    def success_series(self, start_ts: int, end_ts: int, by: Optional[str] = None,
                       step_ms: int = 5000, technical_only: bool = False) -> dict:
        """by in {None,'bank','service','code'}. -> {'series': {key: [[ts, rate], ...]}}"""

    def decline_breakdown(self, start_ts: int, end_ts: int, by: Optional[str] = None) -> dict:
        """-> {'technical_pct','business_pct','top_codes':[{code,pct}], 'by': {key: {...}}}"""

    def bank_stats(self, start_ts: int, end_ts: int) -> dict:
        """-> {bank: {'success_rate','technical_success_rate','p95_ms','tps','error_5xx_pct'}}"""

    def query_logs(self, start_ts: int, end_ts: int, service: Optional[str] = None,
                   bank: Optional[str] = None, code: Optional[str] = None, limit: int = 20) -> dict:
        """-> {'lines': [{'ts','service','bank','level','code','msg'}], 'total': int}"""

    def deploys(self, start_ts: int, end_ts: int) -> list[dict]:
        """Deploy/config events (contracts Deploy shape)."""

    def get_deploy(self, deploy_id: str) -> Optional[dict]: ...

    def traffic(self, start_ts: int, end_ts: int) -> dict:
        """-> {'tps_series': [[ts,v]], 'baseline_tps': float, 'ratio': float}"""

    def read_bulletin(self, bank: str) -> Optional[dict]:
        """Returns {'png_path','image_url'} or None if no bulletin is published for that bank."""

    def bulletin_truth_window(self, bank: str) -> Optional[tuple[int, int]]:
        """Telemetry-derived (start_ts,end_ts) of elevated failures for the bank, or None."""


@runtime_checkable
class World(Protocol):
    """Control surface of the simulation used by the agent/orchestrator."""

    telemetry: Telemetry

    def fork(self) -> "World":
        """Independent deep copy (config, RNG state, clock, buffers). Must not touch the live sim."""

    def run_fast(self, seconds: float) -> None:
        """Advance this world's clock by `seconds` of sim time as fast as possible."""

    def apply_action(self, kind: str, params: dict) -> dict:
        """Whitelisted remediation. kind in contracts ActionKind. -> {'ok': bool, 'detail': str}"""

    def preview_action(self, kind: Optional[str], params: dict, horizon_s: int = 120) -> dict:
        """Run fork with and without the action. -> {'recovers','expected_recovery_ms',
        'predicted':[[ts,v]], 'baseline':[[ts,v]]}. Budget < 300ms wall."""

    def success_rate_now(self, window_s: int = 30) -> float: ...

    def ground_truth(self) -> Optional[dict]:
        """{'scenario','cause','fault_start_ts'} -- used ONLY by eval/chaos reveal, never by the agent."""
