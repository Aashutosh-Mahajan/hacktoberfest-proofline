"""Stable telemetry hash for determinism tests."""
from __future__ import annotations
import hashlib
import numpy as np


def telemetry_hash(arrays: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(arrays):
        a = np.ascontiguousarray(np.asarray(arrays[k]))
        if a.dtype.kind == "f":
            a = a.astype("<f8")
        h.update(k.encode()); h.update(b"|"); h.update(str(a.dtype).encode())
        h.update(b"|"); h.update(str(a.shape).encode()); h.update(b"|")
        h.update(a.tobytes()); h.update(b";")
    return h.hexdigest()
