"""Small helpers: loose JSON parsing, truncation, language detection, constants."""
from __future__ import annotations

import json
import re
from typing import Any

PATH_SERVICES = ("gateway", "payment-orchestrator", "upi-service", "bank-adapter")
DEPLOY_TOLERANCE_MS = 15_000          # deploy may trail a back-dated dip start by this much
DEPLOY_LOOKBACK_MS = 15 * 60_000
ASSUMED_MANUAL_MTTR_MS = 25 * 60_000
AVG_TICKET_PAISE = 45_000


def on_path(service: str) -> bool:
    return any(service == p or service.startswith(p) for p in PATH_SERVICES)


def trunc(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_json_loose(text: Any) -> Any:
    """Parse JSON from model output: strips fences, extracts first balanced object, fixes trailing commas/quotes."""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    cands = [t]
    idx = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if idx:
        start = min(idx)
        open_c = t[start]
        close_c = "}" if open_c == "{" else "]"
        depth = 0
        for i in range(start, len(t)):
            if t[i] == open_c:
                depth += 1
            elif t[i] == close_c:
                depth -= 1
                if depth == 0:
                    cands.append(t[start:i + 1])
                    break
        else:
            cands.append(t[start:] + close_c * max(depth, 0))
    fixes = (lambda s: s,
             lambda s: re.sub(r",\s*([}\]])", r"\1", s),
             lambda s: re.sub(r",\s*([}\]])", r"\1", s).replace("'", '"'))
    for c in cands:
        for fix in fixes:
            try:
                return json.loads(fix(c))
            except Exception:
                continue
    return None


def detect_lang(text: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & {"ka", "ki", "ke", "kya", "hai", "pe", "koi", "asar", "scene", "hua", "kyun", "nahi", "mein"}:
        return "hinglish"
    return "en"


class NormTelemetry:
    """Adapter: the agent works in fractions (0-1). The sim reports percent (0-100); normalize rates here."""

    def __init__(self, inner):
        self._i = inner

    def __getattr__(self, name):
        return getattr(self._i, name)

    @staticmethod
    def _f(v):
        return v / 100.0 if isinstance(v, (int, float)) and v > 1.5 else v

    def bank_stats(self, start_ts, end_ts):
        out = self._i.bank_stats(start_ts, end_ts)
        return {b: {**s, "success_rate": self._f(s.get("success_rate", 0)),
                    "technical_success_rate": self._f(s.get("technical_success_rate", 0))} for b, s in out.items()}

    def success_series(self, start_ts, end_ts, by=None, step_ms=5000, technical_only=False):
        r = self._i.success_series(start_ts, end_ts, by, step_ms, technical_only)
        r = dict(r)
        r["series"] = {k: [[t, self._f(v)] for t, v in pts] for k, pts in r.get("series", {}).items()}
        return r


class NormWorld:
    def __init__(self, inner):
        self._i = inner
        self.telemetry = NormTelemetry(inner.telemetry)

    def __getattr__(self, name):
        return getattr(self._i, name)

    def success_rate_now(self, window_s=30):
        v = self._i.success_rate_now(window_s)
        return v / 100.0 if v > 1.5 else v
