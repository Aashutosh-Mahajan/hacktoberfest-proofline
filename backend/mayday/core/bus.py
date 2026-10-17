"""Thread-safe event bus with a replay ring buffer. Implements core.protocols.EventBus."""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Optional

CONTRACT_VERSION = 2


class Bus:
    def __init__(self, ring: int = 5000, clock: Optional[Callable[[], int]] = None):
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=ring)
        self._subs: list[Callable[[dict], None]] = []
        self._clock = clock or (lambda: 0)

    def set_clock(self, clock: Callable[[], int]) -> None:
        self._clock = clock

    def emit(self, type: str, data: dict, ts: Optional[int] = None) -> int:
        with self._lock:
            self._seq += 1
            env = {"v": CONTRACT_VERSION, "seq": self._seq,
                   "ts": int(ts if ts is not None else self._clock()), "type": type, "data": data}
            self._ring.append(env)
            subs = list(self._subs)
        for s in subs:
            try:
                s(env)
            except Exception:  # a bad subscriber must never break the pipeline
                pass
        return env["seq"]

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)
        def unsub() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsub

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._ring if e["seq"] > seq]

    @property
    def seq(self) -> int:
        return self._seq

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()
