/** Formatting helpers. Backend rates (success_rate, *_pct, by_bank) are PERCENT (0..100). */

export const pct = (v: number | undefined | null): number => (v == null || Number.isNaN(v) ? NaN : v);

/** Fraction (0..1) -> percent, for the few contract fields that are fractions (confidence, eval accuracy). */
export const frac = (v: number | undefined | null): number => (v == null || Number.isNaN(v) ? NaN : v * 100);

export const fmtPct = (v: number | undefined | null, digits = 1): string => {
  const p = pct(v);
  return Number.isNaN(p) ? "—" : `${p.toFixed(digits)}%`;
};

/** Paise → Indian-grouped rupees, compact with L / Cr for big numbers. */
export function fmtRupees(paise: number | undefined | null, compact = true): string {
  if (paise == null || Number.isNaN(paise)) return "₹—";
  const r = paise / 100;
  const abs = Math.abs(r);
  if (compact) {
    if (abs >= 1e7) return `₹${(r / 1e7).toFixed(2)} Cr`;
    if (abs >= 1e5) return `₹${(r / 1e5).toFixed(2)} L`;
  }
  return `₹${Math.round(r).toLocaleString("en-IN")}`;
}

export function fmtDuration(ms: number | undefined | null): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

export function fmtClock(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

export const fmtInt = (n: number | undefined | null): string =>
  n == null || Number.isNaN(n) ? "—" : Math.round(n).toLocaleString("en-IN");

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const confidencePct = (c: number): number => Math.round(c <= 1 ? c * 100 : c); // confidence is 0..1 on the wire

export const SCENARIO_LABEL: Record<string, string> = {
  S0: "No fault",
  S1: "Bad deploy",
  S2: "Bank outage",
  S3: "Decoy deploy + bank outage",
  S4: "Traffic spike",
  S5: "Business-decline surge",
  S6: "Compound fault",
};

export const ACTION_LABEL: Record<string, string> = {
  rollback_deploy: "Roll back deploy",
  failover_bank: "Fail over bank",
  enable_circuit_breaker: "Enable circuit breaker",
  scale_gateway: "Scale gateway",
  notify_only: "Notify only",
};

export const STATE_LABEL: Record<string, string> = {
  idle: "Watching",
  triage: "Triage",
  investigating: "Investigate",
  concluding: "Conclude",
  verifying: "Verify",
  previewing: "Preview",
  proposing: "Propose",
  awaiting_approval: "Approval",
  remediating: "Remediate",
  comms: "Comms",
  postmortem: "Postmortem",
  inconclusive: "Inconclusive",
  error: "Error",
};
