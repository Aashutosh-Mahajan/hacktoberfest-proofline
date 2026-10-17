"""Telemetry (read-only view) implementation. All rates are PERCENT (0-100)."""
from __future__ import annotations

import bisect
from pathlib import Path
import tempfile
from typing import Optional

import numpy as np

from mayday.core.protocols import BANKS
from mayday.sim.engine import (CODES, EDGES, NB, SERVICES, T0, TICK, Engine)


def p95_from_hist(h: np.ndarray, q: float = 0.95) -> float:
    tot = h.sum()
    if tot <= 0:
        return 0.0
    c = np.cumsum(h)
    i = int(np.searchsorted(c, q * tot))
    i = min(i, NB - 1)
    lo = EDGES[i - 1] if i > 0 else 5.0
    hi = EDGES[i] if i < len(EDGES) else EDGES[-1] * 1.3
    prev = c[i - 1] if i > 0 else 0
    frac = (q * tot - prev) / max(h[i], 1)
    return float(lo * (hi / lo) ** min(max(frac, 0), 1))


def _r(x, n=3):
    return round(float(x), n)


class SimTelemetry:
    def __init__(self, engine: Engine):
        self.e = engine

    def now(self) -> int:
        return int(self.e.now)

    # ------------------------------------------------------------ binning
    def _bins(self, start_ts, end_ts, step_ms):
        e = self.e
        with e.lock:
            k0 = max(e.ts_to_tick(start_ts), e.lo)
            k1 = min(e.ts_to_tick(end_ts), e.tick)
            if k1 <= k0:
                return [], None, None, None
            step = max(1, int(step_ms // TICK))
            k0, cnt, pz, side = e.range_arrays(k0, k1)
            offs = np.arange(0, len(cnt), step)
            c = np.add.reduceat(cnt, offs, axis=0).astype(np.int64)
            p = np.add.reduceat(pz, offs, axis=0)
            s = np.add.reduceat(side, offs, axis=0)
            ts = [int(T0 + (k0 + o) * TICK) for o in offs]
            return ts, c, p, s

    def success_series(self, start_ts, end_ts, by=None, step_ms=5000, technical_only=False):
        ts, c, p, s = self._bins(start_ts, end_ts, step_ms)
        out = {}
        if not ts:
            return {"series": {}, "step_ms": step_ms, "unit": "percent"}

        def rate(tot, succ, biz):
            den = tot - biz if technical_only else tot
            return np.where(den > 0, 100.0 * succ / np.maximum(den, 1), np.nan)

        def pack(vals):
            return [[t, _r(v)] for t, v in zip(ts, vals) if not np.isnan(v)]

        if by in (None, "all"):
            a = c.sum(1)
            out["all"] = pack(rate(a[:, 0], a[:, 1], a[:, 5:8].sum(1)))
        elif by == "bank":
            for i, b in enumerate(BANKS):
                out[b] = pack(rate(c[:, i, 0], c[:, i, 1], c[:, i, 5:8].sum(1)))
        elif by == "service":
            a = c.sum(1)
            tot = a[:, 0].astype(float)
            h1 = tot - a[:, 4]
            out["gateway"] = pack(np.where(tot > 0, 100 * (tot - a[:, 4]) / np.maximum(tot, 1), np.nan))
            out["payment-orchestrator"] = pack(np.where(h1 > 0, 100 * (h1 - a[:, 2]) / np.maximum(h1, 1), np.nan))
            out["upi-service"] = pack(np.where(h1 > 0, 100.0, np.nan))
            out["bank-adapter"] = pack(np.where(h1 > 0, 100 * (h1 - a[:, 3]) / np.maximum(h1, 1), np.nan))
            ss = s.astype(float)
            out["wallet"] = pack(np.where(ss[:, 0] > 0, 100 * (ss[:, 0] - ss[:, 1]) / np.maximum(ss[:, 0], 1), np.nan))
            out["notification-service"] = pack(np.where(ss[:, 2] > 0, 100 * (ss[:, 2] - ss[:, 3]) / np.maximum(ss[:, 2], 1), np.nan))
        elif by == "code":
            a = c.sum(1)
            for j, code in enumerate(CODES):
                out[code] = pack(np.where(a[:, 0] > 0, 100.0 * a[:, 2 + j] / np.maximum(a[:, 0], 1), np.nan))
        else:
            raise ValueError(f"unsupported by={by!r}")
        return {"series": out, "step_ms": step_ms, "unit": "percent"}

    # ------------------------------------------------------------ declines
    @staticmethod
    def _bd(row: np.ndarray) -> dict:
        d = row[2:8].astype(float)
        tot = d.sum()
        tech = d[:3].sum()
        if tot <= 0:
            return {"technical_pct": 0.0, "business_pct": 0.0, "top_codes": [], "declines": 0}
        top = sorted(((CODES[i], _r(100 * d[i] / tot, 2)) for i in range(6) if d[i] > 0), key=lambda x: -x[1])
        return {"technical_pct": _r(100 * tech / tot, 2), "business_pct": _r(100 * (tot - tech) / tot, 2),
                "top_codes": [{"code": c, "pct": p} for c, p in top[:4]], "declines": int(tot)}

    def decline_breakdown(self, start_ts, end_ts, by=None):
        e = self.e
        with e.lock:
            a = e.sum_range(e.ts_to_tick(start_ts), e.ts_to_tick(end_ts))
        out = self._bd(a.sum(0))
        out["total_txns"] = int(a[:, 0].sum())
        if by == "bank":
            out["by"] = {b: self._bd(a[i]) for i, b in enumerate(BANKS)}
        elif by == "code":
            out["by"] = {c: {"count": int(a[:, 2 + j].sum())} for j, c in enumerate(CODES)}
        else:
            out["by"] = {}
        return out

    def bank_stats(self, start_ts, end_ts):
        e = self.e
        with e.lock:
            a = e.sum_range(e.ts_to_tick(start_ts), e.ts_to_tick(end_ts))
        secs = max((end_ts - start_ts) / 1000.0, 0.001)
        out = {}
        for i, b in enumerate(BANKS):
            r = a[i]
            tot = max(int(r[0]), 1)
            biz = int(r[5:8].sum())
            out[b] = {"success_rate": _r(100 * r[1] / tot), "technical_success_rate": _r(100 * r[1] / max(tot - biz, 1)),
                      "p95_ms": _r(p95_from_hist(r[8:]), 1), "tps": _r(r[0] / secs, 1),
                      "error_5xx_pct": _r(100 * r[3] / tot, 2), "timeout_pct": _r(100 * r[2] / tot, 2),
                      "total": int(r[0])}
        return out

    # ------------------------------------------------------------ logs / deploys
    def query_logs(self, start_ts, end_ts, service=None, bank=None, code=None, limit=20):
        e = self.e
        with e.lock:
            i0 = bisect.bisect_left(e.log_ts, start_ts)
            i1 = bisect.bisect_right(e.log_ts, min(end_ts, e.now))
            m = [l for l in e.logs[i0:i1]
                 if (service is None or l[1] == service) and (bank is None or l[2] == bank) and (code is None or l[4] == code)]
        sel = m[-limit:]
        return {"lines": [{"ts": l[0], "service": l[1], "bank": l[2], "level": l[3], "code": l[4], "msg": l[5]} for l in sel],
                "total": len(m)}

    def deploys(self, start_ts, end_ts):
        now = self.now()
        return [dict(d) for d in self.e.deploys if start_ts <= d["ts"] <= min(end_ts, now)]

    def get_deploy(self, deploy_id):
        digits = "".join(ch for ch in str(deploy_id) if ch.isdigit())
        now = self.now()
        for d in self.e.deploys:
            if str(d["number"]) == digits and d["ts"] <= now:
                return dict(d)
        return None

    def traffic(self, start_ts, end_ts):
        ts, c, p, s = self._bins(start_ts, end_ts, 5000)
        series = [[t, _r(c[i].sum(0)[0] / 5.0, 1)] for i, t in enumerate(ts)]
        e = self.e
        with e.lock:
            kb1 = e.ts_to_tick(start_ts) - 240
            kb0 = kb1 - 1200
            if kb1 - max(kb0, e.lo) < 80:
                kb0, kb1 = e.lo, e.lo + 1200
            a = e.sum_range(kb0, kb1)
            base = a[:, 0].sum() / max((min(kb1, e.tick) - max(kb0, e.lo)) * TICK / 1000.0, 1)
        cur = float(np.mean([v for _, v in series])) if series else 0.0
        return {"tps_series": series, "baseline_tps": _r(base, 1), "ratio": _r(cur / base if base else 0, 3)}

    # ------------------------------------------------------------ bulletin
    def read_bulletin(self, bank):
        spec = self.e.bulletins.get(bank)
        if not spec or self.now() < spec["publish_ts"]:
            return None
        from mayday.sim.bulletin import render_bulletin
        d = Path(tempfile.gettempdir()) / "mayday_bulletins"
        path = d / f"{bank}_{self.e.p.scenario}_{self.e.p.seed}.png"
        if not path.exists():
            render_bulletin(bank, spec["headline"], spec["status"], spec["claimed_start_ts"] // 1000,
                            spec["claimed_end_ts"] // 1000, path)
        extracted = {k: spec[k] for k in ("bank", "status", "claimed_start_ts", "claimed_end_ts")}
        return {"png_path": str(path), "image_url": f"/api/bulletin/{bank}.png", "extracted": extracted,
                "headline": spec["headline"], "published_ts": spec["publish_ts"]}

    def bulletin_truth_window(self, bank):
        if bank not in BANKS:
            return None
        e = self.e
        i = BANKS.index(bank)
        with e.lock:
            k0 = max(e.lo, e.tick - 2400)
            k0, cnt, _, _ = e.range_arrays(k0, e.tick)
        if len(cnt) < 20:
            return None
        n = len(cnt) // 20
        c = cnt[: n * 20, i].reshape(n, 20, -1).sum(1)
        tot = c[:, 0]
        fail = (c[:, 2] + c[:, 3] + c[:, 4]).astype(float)
        rate = np.where(tot > 10, 100 * fail / np.maximum(tot, 1), 0)
        bad = np.nonzero(rate > 5.0)[0]
        if len(bad) == 0:
            return None
        # most recent contiguous-ish run (gaps <=3 bins merge)
        end = bad[-1]
        st = end
        for j in bad[::-1]:
            if st - j <= 3:
                st = j
            else:
                break
        t_start = int(T0 + (k0 + st * 20) * TICK)
        t_end = int(T0 + (k0 + (end + 1) * 20) * TICK)
        if t_end >= self.now() - 10_000:
            t_end = self.now()
        return (t_start, t_end)
