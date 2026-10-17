"""Vectorized, deterministic payments simulator.

Determinism: randomness for tick k comes from a counter-based Philox stream keyed by (seed, k), so
results are independent of how steps are batched and a fork replays the exact same future.
All rates exposed through telemetry are PERCENT (0-100). Money is integer paise.
"""
from __future__ import annotations

import copy
import math
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from mayday.core.protocols import BANKS

TICK = 250                      # ms per tick
T0 = 1_760_000_000_000          # sim epoch (ms) at history start
PREROLL_MS = 300_000            # healthy history generated at reset
L0 = T0 + PREROLL_MS            # live start
BASE_RATE = 320.0               # txn/s
W = np.array([0.40, 0.35, 0.25])
MEDIAN = np.array([600.0, 250.0, 300.0])
SIGMA = np.array([0.5, 0.45, 0.45])
P5_BASE = 0.001
BIZ_BASE = (0.005, 0.003, 0.001)   # wrong pin, insufficient funds, limit exceeded
GW_CAP = 480.0
TMO_BASE = 3000.0
AVG_PAISE = 45000
TECH = ("TD_TIMEOUT", "TD_BANK_5XX", "TD_RATE_LIMITED")
BIZ = ("BD_WRONG_PIN", "BD_INSUFFICIENT_FUNDS", "BD_LIMIT_EXCEEDED")
CODES = TECH + BIZ
EDGES = np.geomspace(20, 8000, 37)
NB = len(EDGES) + 1
NF = 8 + NB                     # total, success, 3 tech, 3 biz, hist
SERVICES = ("gateway", "payment-orchestrator", "upi-service", "bank-adapter", "wallet", "notification-service")
ACTIONS = ("rollback_deploy", "failover_bank", "enable_circuit_breaker", "scale_gateway", "notify_only")


def ramp(t, t0, dur):
    return np.clip((t - t0) / float(dur), 0.0, 1.0)


@dataclass
class Params:
    seed: int = 1
    scenario: str = "S0"
    fault_offset_s: float = 15.0
    severity: float = 1.0


def _deploys(p: Params, f0: int) -> list:
    def d(n, svc, ts, au, s, diff):
        return dict(id=f"deploy:{n}", service=svc, number=n, ts=int(ts), author=au, summary=s, diff=diff)
    out = [
        d(478, "wallet-service", L0 - 200 * 60_000, "meera.k", "Wallet balance cache TTL 30s -> 60s", "-  ttl: 30\n+  ttl: 60"),
        d(479, "notification-service", L0 - 95 * 60_000, "dev.s", "Add SMS retry jitter", "-  retry_jitter: false\n+  retry_jitter: true"),
        d(480, "gateway", L0 - 52 * 60_000, "ankit.p", "Rotate TLS certificates", "-  cert: 2025-q3\n+  cert: 2025-q4"),
        d(481, "upi-service", L0 - 38 * 60_000, "sara.n", "Structured logging for collect flow", "-  log_format: text\n+  log_format: json"),
    ]
    sc = p.scenario
    if sc in ("S1", "S6"):
        t1 = int(800 / max(p.severity, 0.3))
        out.append(d(482, "payment-orchestrator", f0, "rohan.m", "Tune bank adapter timeouts for latency SLO",
                     "--- a/config/orchestrator.yaml\n+++ b/config/orchestrator.yaml\n@@ bank_timeouts @@\n   BANK_X:\n"
                     f"-    timeout_ms: 3000\n+    timeout_ms: {t1}\n   BANK_Y:\n     timeout_ms: 3000\n   BANK_Z:\n     timeout_ms: 3000"))
    if sc == "S3":
        out.append(d(483, "notification-service", f0 + 40_000 + 3_000, "dev.s", "Increase push batch size 50 -> 200",
                     "-  push_batch_size: 50\n+  push_batch_size: 200"))
    return sorted(out, key=lambda x: x["ts"])


def _faults(p: Params, f0: int) -> list:
    s = p.severity
    sc = p.scenario
    F = []
    if sc in ("S1", "S6"):
        F.append(dict(kind="timeout", bank=0, start=f0 + 2000, ramp=3000, end=None, deploy=482, t1=max(450.0, 800.0 / max(s, 0.3))))
    if sc in ("S2", "S3"):
        F.append(dict(kind="bank", bank=0, start=f0, ramp=3000, end=f0 + 420_000, deploy=None,
                      lat=1 + 5 * s, p5=min(0.6, 0.001 + 0.179 * s)))
    if sc == "S5":
        F.append(dict(kind="biz", start=f0, ramp=5000, end=f0 + 480_000, deploy=None, p=min(0.6, 0.2 * s)))
    if sc == "S4":
        F.append(dict(kind="traffic", start=f0, ramp=4000, end=f0 + 240_000, deploy=None, mult=1 + 2 * s))
    if sc == "S6":
        F.append(dict(kind="bank", bank=1, start=f0 + 5000, ramp=3000, end=None, deploy=None, lat=2.0, p5=0.06))
    if sc == "S3":
        F.append(dict(kind="notif", start=f0 + 43_000, ramp=3000, end=None, deploy=483))
    return F


def _cause(p: Params):
    return {"S1": "deploy:482", "S2": "bank:BANK_X", "S3": "bank:BANK_X", "S5": "business:limit_exceeded",
            "S4": "traffic_spike", "S6": "deploy:482"}.get(p.scenario)


class Engine:
    def __init__(self, seed=1, scenario="S0", fault_offset_s=15.0, severity=1.0, cap=12000,
                 logs=True, _blank=False):
        self.lock = threading.RLock()
        self.p = Params(int(seed), scenario, float(fault_offset_s), float(severity))
        if _blank:
            return
        self.cap = cap
        self.logs_on = logs
        self.f0 = int(L0 + self.p.fault_offset_s * 1000)
        self.faults = _faults(self.p, self.f0)
        self.deploys = _deploys(self.p, self.f0)
        self.cfg = dict(rollback={}, failover={}, breaker={}, gw=None)
        self.cfg_version = 0
        self.cnt = np.zeros((cap, 3, NF), np.int32)
        self.pz = np.zeros((cap, 3, 2), np.int64)      # failed paise, total paise
        self.side = np.zeros((cap, 4), np.int32)       # wallet tot/fail, notif tot/fail
        self.tick = 0
        self.lo = 0
        self.log_ts: list = []
        self.logs: list = []
        self.ref_success = 99.0
        self.ref_tsr = 99.8
        self._rng_noise = np.random.default_rng([self.p.seed, 77])
        self.bulletins = self._bulletin_specs()
        self.step(int(PREROLL_MS / TICK))
        a = self.sum_range(0, self.tick).sum(0)
        self.ref_success = 100.0 * a[1] / max(a[0], 1)
        self.ref_tsr = 100.0 * a[1] / max(a[0] - a[5:8].sum(), 1)

    # ---------------------------------------------------------------- time
    @property
    def now(self) -> int:
        return T0 + self.tick * TICK

    def _key(self):
        return (self.p.seed * 2654435761 + 12345) & (2**63 - 1)

    # ---------------------------------------------------------------- tables
    def _tables(self, tk):
        t = (T0 + tk * TICK).astype(np.float64)
        nt = len(t)
        lam = BASE_RATE * (1 + 0.04 * np.sin(2 * np.pi * (t - T0) / 600_000.0)) * TICK / 1000.0
        tmo = np.full((nt, 3), TMO_BASE)
        latm = np.ones((nt, 3))
        p5 = np.full((nt, 3), P5_BASE)
        plim = np.full(nt, BIZ_BASE[2])
        tm = np.ones(nt)
        notif = np.zeros(nt)
        for f in self.faults:
            s = ramp(t, f["start"], f["ramp"])
            if f["end"]:
                s = s * (1 - ramp(t, f["end"], 5000))
            rb = self.cfg["rollback"].get(f["deploy"]) if f["deploy"] else None
            if rb is not None:
                s = s * (1 - ramp(t, rb, 4000))
            k = f["kind"]
            if k == "timeout":
                tmo[:, f["bank"]] = TMO_BASE - (TMO_BASE - f["t1"]) * s
            elif k == "bank":
                latm[:, f["bank"]] *= 1 + (f["lat"] - 1) * s
                p5[:, f["bank"]] += (f["p5"] - P5_BASE) * s
            elif k == "biz":
                plim += f["p"] * s
            elif k == "traffic":
                tm *= 1 + (f["mult"] - 1) * s
            elif k == "notif":
                notif += s
        lam = lam * tm
        latm *= (1 + 0.15 * (tm - 1))[:, None]
        w = np.tile(W, (nt, 1))
        for b, ts in self.cfg["failover"].items():
            w[:, BANKS.index(b)] *= 1 - ramp(t, ts, 5000)
        w /= w.sum(1, keepdims=True)
        cw = np.cumsum(w, 1)
        cap = np.full(nt, GW_CAP)
        if self.cfg["gw"] is not None:
            cap = GW_CAP * (1 + 2 * ramp(t, self.cfg["gw"], 6000))
        prl = np.clip(1 - cap / (lam / (TICK / 1000.0)), 0, 0.95)
        cb = np.zeros((nt, 3), bool)
        for b, ts in self.cfg["breaker"].items():
            cb[:, BANKS.index(b)] = t >= ts + 2000
        return t, lam, tmo, latm, p5, plim, prl, cw, cb, notif

    # ---------------------------------------------------------------- step
    def step(self, nt: int) -> None:
        if nt <= 0:
            return
        with self.lock:
            k0 = self.tick
            tk = np.arange(k0, k0 + nt)
            t, lam, tmo, latm, p5, plim, prl, cw, cb, notif = self._tables(tk)
            key = self._key()
            ns = np.empty(nt, np.int64)
            Us, Zs, Xs = [], [], []
            for i in range(nt):
                g = np.random.Generator(np.random.Philox(key=key, counter=int(tk[i]) << 32))
                n = int(g.poisson(lam[i]))
                ns[i] = n
                Us.append(g.random((7, n)))
                Zs.append(g.standard_normal((2, n)))
                Xs.append(g.poisson([10.0, 0.02, 12.0, 0.06 + 0.5 * notif[i]]))
            U = np.concatenate(Us, 1)
            Z = np.concatenate(Zs, 1)
            N = U.shape[1]
            ti = np.repeat(np.arange(nt), ns)
            bank = (U[1] > cw[ti, 0]).astype(np.int64) + (U[1] > cw[ti, 1])
            lat = MEDIAN[bank] * latm[ti, bank] * np.exp(SIGMA[bank] * Z[0])
            paise = np.maximum(100, np.rint(np.exp(np.log(32600) + 0.8 * Z[1]))).astype(np.int64)
            rl = U[2] < prl[ti]
            e5 = ~rl & (U[3] < p5[ti, bank])
            to = ~rl & ~e5 & (lat > tmo[ti, bank])
            ok = ~(rl | e5 | to)
            pl = plim[ti]
            b0, b1 = BIZ_BASE[0], BIZ_BASE[1]
            u = U[4]
            bw = ok & (u < b0)
            bi = ok & (u >= b0) & (u < b0 + b1)
            bl = ok & (u >= b0 + b1) & (u < b0 + b1 + pl)
            succ = ok & ~(bw | bi | bl)
            lat_o = lat.copy()
            lat_o[rl] = 5.0
            lat_o[e5] = lat[e5] * 0.5
            lat_o[to] = tmo[ti, bank][to]
            fast = (e5 | to) & cb[ti, bank]
            lat_o[fast] = 40.0
            flags = [np.ones(N, bool), succ, to, e5, rl, bw, bi, bl]
            nk = nt * 3
            key_tb = ti * 3 + bank
            cnt = np.zeros((nk, NF), np.int64)
            for j, fl in enumerate(flags):
                cnt[:, j] = np.bincount(key_tb[fl], minlength=nk)
            hb = np.searchsorted(EDGES, lat_o)
            cnt[:, 8:] = np.bincount(key_tb * NB + hb, minlength=nk * NB).reshape(nk, NB)
            pz = np.zeros((nk, 2), np.int64)
            pz[:, 0] = np.rint(np.bincount(key_tb, weights=np.where(succ, 0, paise), minlength=nk)).astype(np.int64)
            pz[:, 1] = np.rint(np.bincount(key_tb, weights=paise, minlength=nk)).astype(np.int64)
            idx = tk % self.cap
            self.cnt[idx] = cnt.reshape(nt, 3, NF)
            self.pz[idx] = pz.reshape(nt, 3, 2)
            X = np.array(Xs)
            self.side[idx] = X
            if self.logs_on:
                self._gen_logs(t, ti, bank, U, tmo, flags, notif, k0, nt)
            self.tick = k0 + nt
            self.lo = max(self.lo, self.tick - self.cap)

    # ---------------------------------------------------------------- logs
    def _gen_logs(self, t, ti, bank, U, tmo, flags, notif, k0, nt):
        rng = self._rng_noise
        lines = []
        samp = U[5] < 0.25
        _, succ, to, e5, rl, bw, bi, bl = flags
        for mask, code in ((to, "TD_TIMEOUT"), (e5, "TD_BANK_5XX"), (rl, "TD_RATE_LIMITED"),
                           (bw, "BD_WRONG_PIN"), (bi, "BD_INSUFFICIENT_FUNDS"), (bl, "BD_LIMIT_EXCEEDED")):
            for i in np.nonzero(mask & samp)[0][:400]:
                tt = int(t[ti[i]] + U[0][i] * TICK)
                bk = BANKS[bank[i]]
                tid = f"txn_{int(k0 + ti[i])}_{int(i)}"
                if code == "TD_TIMEOUT":
                    ln = (tt, "payment-orchestrator", bk, "ERROR", code,
                          f"upstream timeout calling bank-adapter[{bk}] after {int(tmo[ti[i], bank[i]])}ms ({tid})")
                elif code == "TD_BANK_5XX":
                    ln = (tt, "bank-adapter", bk, "ERROR", code,
                          f"{bk} responded HTTP {('503', '502', '500')[int(U[6][i] * 3)]} from upstream switch ({tid})")
                elif code == "TD_RATE_LIMITED":
                    ln = (tt, "gateway", bk, "WARN", code, f"rate limit exceeded, request rejected ({tid})")
                elif code == "BD_LIMIT_EXCEEDED":
                    ln = (tt, "upi-service", bk, "WARN", code, f"issuer declined: daily transaction limit exceeded ({tid})")
                elif code == "BD_WRONG_PIN":
                    ln = (tt, "upi-service", bk, "INFO", code, f"issuer declined: incorrect UPI PIN ({tid})")
                else:
                    ln = (tt, "upi-service", bk, "INFO", code, f"issuer declined: insufficient funds ({tid})")
                lines.append(ln)
        noise = [("gateway", "health check ok", "INFO"), ("wallet", "balance cache refreshed", "INFO"),
                 ("notification-service", "push batch delivered", "INFO"),
                 ("upi-service", "slow query 230ms on vpa_lookup", "WARN"),
                 ("payment-orchestrator", "config reload ok", "INFO")]
        for _ in range(int(rng.poisson(1.0 * nt))):
            svc, msg, lvl = noise[int(rng.integers(0, len(noise)))]
            lines.append((int(T0 + (k0 + rng.random() * nt) * TICK), svc, None, lvl, None, msg))
        if notif.max() > 0.05:
            for _ in range(int(rng.poisson(2.0 * nt * notif.mean()))):
                lines.append((int(T0 + (k0 + rng.random() * nt) * TICK), "notification-service", None, "WARN", None,
                              "push provider latency high, batch of 200 delayed"))
        for d in self.deploys:
            if T0 + k0 * TICK <= d["ts"] < T0 + (k0 + nt) * TICK:
                lines.append((d["ts"], d["service"], None, "INFO", None, f"deploy #{d['number']} rolled out: {d['summary']}"))
        lines.sort(key=lambda x: x[0])
        self.logs.extend(lines)
        self.log_ts.extend(l[0] for l in lines)
        if len(self.logs) > 80_000:
            self.logs = self.logs[-60_000:]
            self.log_ts = self.log_ts[-60_000:]

    # ---------------------------------------------------------------- ranges
    def sum_range(self, k0: int, k1: int) -> np.ndarray:
        """counts summed over ticks -> (3, NF)."""
        k0 = max(k0, self.lo)
        k1 = min(k1, self.tick)
        if k1 <= k0:
            return np.zeros((3, NF), np.int64)
        return self.cnt[np.arange(k0, k1) % self.cap].sum(0, dtype=np.int64)

    def range_arrays(self, k0: int, k1: int):
        k0 = max(k0, self.lo)
        k1 = min(k1, self.tick)
        k1 = max(k1, k0)
        idx = np.arange(k0, k1) % self.cap
        return k0, self.cnt[idx], self.pz[idx], self.side[idx]

    def ts_to_tick(self, ts: int) -> int:
        return int(math.floor((ts - T0) / TICK))

    # ---------------------------------------------------------------- actions
    def apply_action(self, kind: str, params: dict) -> dict:
        params = params or {}
        with self.lock:
            now = self.now
            if kind == "rollback_deploy":
                raw = str(params.get("deploy_id") or params.get("deploy") or params.get("number") or "")
                digits = "".join(c for c in raw if c.isdigit())
                d = next((x for x in self.deploys if str(x["number"]) == digits and x["ts"] <= now), None)
                if not d:
                    return {"ok": False, "detail": f"unknown deploy {raw!r}"}
                if d["number"] in self.cfg["rollback"]:
                    return {"ok": True, "detail": f"deploy #{d['number']} already rolled back"}
                self.cfg["rollback"][d["number"]] = now
                self.cfg_version += 1
                return {"ok": True, "detail": f"rolled back deploy #{d['number']} on {d['service']}"}
            if kind == "failover_bank":
                bank = params.get("bank") or params.get("from")
                if bank not in BANKS:
                    return {"ok": False, "detail": f"unknown bank {bank!r}"}
                if bank in self.cfg["failover"]:
                    return {"ok": True, "detail": f"{bank} already failed over"}
                self.cfg["failover"][bank] = now
                self.cfg_version += 1
                return {"ok": True, "detail": f"traffic for {bank} rerouted to remaining banks"}
            if kind == "enable_circuit_breaker":
                bank = params.get("bank")
                if bank not in BANKS:
                    return {"ok": False, "detail": f"unknown bank {bank!r}"}
                self.cfg["breaker"].setdefault(bank, now)
                self.cfg_version += 1
                return {"ok": True, "detail": f"circuit breaker enabled for {bank}"}
            if kind == "scale_gateway":
                if self.cfg["gw"] is None:
                    self.cfg["gw"] = now
                    self.cfg_version += 1
                return {"ok": True, "detail": "gateway capacity scaled 3x"}
            if kind == "notify_only":
                return {"ok": True, "detail": "stakeholders notified; no system change"}
            return {"ok": False, "detail": f"action {kind!r} not whitelisted"}

    # ---------------------------------------------------------------- fork
    def fork(self, keep_ticks: int = 1600, headroom: int = 700) -> "Engine":
        with self.lock:
            e = Engine(self.p.seed, self.p.scenario, self.p.fault_offset_s, self.p.severity, _blank=True)
            e.cap = 4096
            assert keep_ticks + headroom <= e.cap
            e.logs_on = False
            e.f0 = self.f0
            e.faults = self.faults
            e.deploys = self.deploys
            e.cfg = copy.deepcopy(self.cfg)
            e.cfg_version = self.cfg_version
            e.cnt = np.zeros((e.cap, 3, NF), np.int32)
            e.pz = np.zeros((e.cap, 3, 2), np.int64)
            e.side = np.zeros((e.cap, 4), np.int32)
            lo = max(self.lo, self.tick - keep_ticks)
            ks = np.arange(lo, self.tick)
            e.cnt[ks % e.cap] = self.cnt[ks % self.cap]
            e.pz[ks % e.cap] = self.pz[ks % self.cap]
            e.side[ks % e.cap] = self.side[ks % self.cap]
            e.tick = self.tick
            e.lo = lo
            e.log_ts, e.logs = [], []
            e.ref_success, e.ref_tsr = self.ref_success, self.ref_tsr
            e._rng_noise = None
            e.bulletins = self.bulletins
            return e

    # ---------------------------------------------------------------- misc
    def telemetry_hash(self) -> str:
        from mayday.sim.hashing import telemetry_hash
        ks = np.arange(self.lo, self.tick) % self.cap
        return telemetry_hash({"cnt": self.cnt[ks], "pz": self.pz[ks], "side": self.side[ks]})

    def ground_truth(self) -> Optional[dict]:
        if self.p.scenario == "S0":
            return {"scenario": "S0", "cause": None, "causes": [], "fault_start_ts": None}
        causes = ["deploy:482", "bank:BANK_Y"] if self.p.scenario == "S6" else [_cause(self.p)]
        return {"scenario": self.p.scenario, "cause": _cause(self.p), "causes": causes,
                "fault_start_ts": int(self.f0)}

    def _bulletin_specs(self) -> dict:
        if self.p.scenario not in ("S2", "S3"):
            return {}
        wrong = bool(np.random.default_rng(self.p.seed + 5).random() < 0.35)
        mn = 60_000
        publish = self.f0 + 25_000
        if wrong:
            ce = ((self.f0 + 20_000) // mn) * mn
            cs = ce - 8 * mn
            head, status = "Planned maintenance on UPI switch completed, services restored", "operational"
        else:
            cs = (self.f0 // mn) * mn
            ce = -(-(self.f0 + 420_000) // mn) * mn
            head, status = "Intermittent degradation on UPI payments", "degraded"
        return {"BANK_X": dict(bank="BANK_X", status=status, headline=head, claimed_start_ts=int(cs),
                               claimed_end_ts=int(ce), publish_ts=int(publish), wrong=wrong)}


def create_world(seed: int = 1, scenario: str = "S0", fault_offset_s: float = 15.0, severity: float = 1.0):
    """Factory used by eval: returns a fresh World (SimWorld) already pre-rolled to the live start."""
    from mayday.sim.world import SimWorld
    return SimWorld(Engine(seed, scenario, fault_offset_s, severity))
