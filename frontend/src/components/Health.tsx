import { memo } from "react";
import { useStore } from "../store/store";
import { useThrottled } from "../lib/useThrottled";
import { fmtPct, fmtRupees, fmtInt } from "../lib/format";
import { codeLabel, plainify } from "../lib/words";
import { HealthChart } from "./HealthChart";
import type { Bank } from "@contract";

const BANKS: { id: Bank; cls: string }[] = [
  { id: "BANK_X", cls: "bank-x" }, { id: "BANK_Y", cls: "bank-y" }, { id: "BANK_Z", cls: "bank-z" },
];

export type HealthKind = "good" | "bad" | "warn";
/** One place that decides the headline status ("All good" / "Problem detected" / "Fixing"). */
export function useHealthStatus(): { kind: HealthKind; label: string; fixed: boolean } {
  const low = useStore((s) => (s.tick ? s.tick.success_rate < 97 : false));
  const open0 = useStore((s) => !!s.incident && !s.incident.end_ts);
  const agent = useStore((s) => s.agentState);
  const recovered = useStore((s) => !!s.executed?.recovered);
  const open = open0 && !recovered; // recovered payments count as fixed even before the incident is formally closed
  const fixed = recovered;
  if (open && agent === "remediating") return { kind: "warn", label: "Fixing", fixed };
  if (open) return { kind: "bad", label: "Problem detected", fixed };
  if (low) return { kind: "warn", label: "Possible problem", fixed };
  return { kind: "good", label: "All good", fixed };
}

export const Hero = memo(function Hero() {
  const tick = useThrottled((s) => s.tick);
  const incident = useStore((s) => s.incident);
  const saved = useStore((s) => s.totalSavedPaise);
  const status = useHealthStatus();
  const sr = tick?.success_rate;
  const lost = tick?.paise_failed_per_min ?? 0;
  const bad = status.kind === "bad" || status.kind === "warn";
  const headline = status.fixed ? "Fixed. Payments are back to normal."
    : bad && incident ? "Payments are failing: " + plainify(incident.blast.scope)
    : bad ? "Payments are slower than usual."
    : "Everything looks healthy. ProofLine is watching.";
  return (
    <section className="card hero" aria-label="Payment health">
      <div className="headrow">
        <h1>{headline}</h1>
        <div className={"big " + (status.kind === "bad" ? "bad" : "good")} aria-live="off">
          {tick ? fmtPct(sr) : <span className="skel" style={{ display: "inline-block", width: 150, height: 48 }} aria-label="Loading" />}
          <small>{incident && !status.fixed ? `of payments succeed (normally ${fmtPct(incident.baseline_success, 0)})` : "of payments succeed"}</small>
        </div>
      </div>
      <div className="legend" aria-hidden="true">
        <span><i style={{ background: "var(--overall)" }} />All payments</span>
        <span><i style={{ background: "var(--bx)" }} />BANK_X</span>
        <span><i style={{ background: "var(--by)" }} />BANK_Y</span>
        <span><i style={{ background: "var(--bz)" }} />BANK_Z</span>
        <span style={{ color: "var(--twin)" }} title="Digital twin forecast"><i className="dash" />Test of the fix (simulated)</span>
        <span style={{ color: "var(--alert)" }} title="Twin baseline: no action"><i className="dash" />If we do nothing</span>
      </div>
      <HealthChart />
      <div className="statrow">
        <div className="stat"><div className="l" title="paise_failed_per_min">Money being lost per minute</div><div className={"v " + (status.kind === "bad" ? "bad" : "")}>{status.kind === "good" ? <span className="dim" style={{ fontWeight: 500, fontSize: 16 }}>Nothing unusual</span> : fmtRupees(lost)}</div></div>
        <div className="stat"><div className="l">Saved so far</div><div className="v good">{fmtRupees(saved)}</div></div>
        <div className="stat"><div className="l" title="p95 latency">Slow-request time</div><div className="v">{tick ? Math.round(tick.p95_ms).toLocaleString("en-IN") + " ms" : "—"}</div></div>
      </div>
    </section>
  );
});

export const BankTiles = memo(function BankTiles() {
  const tick = useThrottled((s) => s.tick);
  return (
    <section className="card" aria-label="Bank health">
      <h2>How each bank is doing</h2>
      <p className="cap">Share of payments that went through, per bank, right now.</p>
      <div className="tiles">
        {BANKS.map(({ id, cls }) => {
          const b = tick?.by_bank?.[id];
          const bad = b ? b.success_rate < 95 : false;
          return (
            <div key={id} className={"tile" + (bad ? " bad" : "")}>
              <div className={"n " + cls}>{id}</div>
              <div className="v">{fmtPct(b?.success_rate)}</div>
              <div className="s" title="p95 latency"><span className="num">{b ? Math.round(b.p95_ms) : "—"} ms</span> slow-request time · <span className="num">{b ? fmtInt(b.tps) : "—"}</span> per sec</div>
            </div>
          );
        })}
      </div>
    </section>
  );
});

export const WhyFailed = memo(function WhyFailed() {
  const tick = useThrottled((s) => s.tick);
  const incident = useStore((s) => s.incident);
  const sys = tick ? tick.decline_mix.technical_pct : 0;
  const cust = tick ? tick.decline_mix.business_pct : 0;
  const R = 38, C = 2 * Math.PI * R;
  const frac = sys + cust > 0 ? sys / (sys + cust) : 0;
  const codes = incident?.decline_breakdown?.top_codes ?? [];
  return (
    <section className="card" aria-label="Why payments failed">
      <h2>Why payments failed</h2>
      <p className="cap">Splits failures into system problems (ours or the bank's) and customer-side issues such as a wrong PIN.</p>
      <div className="mixrow">
        <svg width="110" height="110" viewBox="0 0 100 100" role="img" aria-label={`System problems ${sys.toFixed(0)} percent, customer-side issues ${cust.toFixed(0)} percent`}>
          <circle cx="50" cy="50" r={R} fill="none" stroke="var(--line2)" strokeWidth="12" />
          <circle cx="50" cy="50" r={R} fill="none" stroke="var(--alert)" strokeWidth="12" strokeLinecap="round" strokeDasharray={`${C * frac} ${C}`} transform="rotate(-90 50 50)" style={{ transition: "stroke-dasharray .3s" }} />
          <text x="50" y="56" textAnchor="middle" fontSize="19" fontWeight="700" fill="var(--text)" fontFamily="JetBrains Mono">{sys.toFixed(0)}%</text>
        </svg>
        <div className="reasons">
          <div className="reason"><span title="technical declines"><i className="legend-i" style={{ background: "var(--alert)" }} />System problems</span><b className="num">{sys.toFixed(0)}%</b></div>
          <div className="reason"><span title="business declines"><i className="legend-i" style={{ background: "var(--line2)" }} />Customer-side issues</span><b className="num">{cust.toFixed(0)}%</b></div>
        </div>
      </div>
      <div className="subhead">Most common reasons</div>
      {codes.length ? codes.slice(0, 4).map((c) => (
        <div key={c.code + c.pct} className="reason" style={{ marginBottom: 8 }}>
          <span title={c.code}>{codeLabel(c.code)}</span><b className="num">{c.pct.toFixed(0)}%</b>
          <div className="bar"><i style={{ width: Math.min(100, c.pct) + "%" }} /></div>
        </div>
      )) : <p className="dim" style={{ margin: 0 }}>Reasons show up when a problem is detected.</p>}
      <div className="sub" style={{ marginTop: 12 }} title="technical_success_rate">System success (customer-side declines excluded): <b className="num">{fmtPct(tick?.technical_success_rate)}</b></div>
    </section>
  );
});
