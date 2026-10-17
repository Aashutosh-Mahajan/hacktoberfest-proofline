"""Evidence registry: every claim must cite ids that resolve here (contract Evidence shape)."""
from __future__ import annotations

import threading
from typing import Optional


class EvidenceStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(self, id: str, kind: str, title: str, body: dict, ts: Optional[int] = None) -> str:
        ev = {"id": id, "kind": kind, "title": title, "body": body}
        if ts is not None:
            ev["ts"] = int(ts)
        with self._lock:
            self._d[id] = ev
        return id

    def get(self, id: str) -> Optional[dict]:
        with self._lock:
            return self._d.get(id)

    def has(self, id: str) -> bool:
        return self.get(id) is not None

    def ids(self) -> list:
        with self._lock:
            return list(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
