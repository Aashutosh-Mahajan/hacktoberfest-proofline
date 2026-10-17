import { useStore } from "../store/store";
import { fmtPct, pct, pctPair, fmtInt } from "../lib/format";
import type { Bank } from "@contract";

const BANKS: { id: Bank; cls: string }[] = [
  { id: "BANK_X", cls: "bank-x" }, { id: "BANK_Y", cls: "bank-y" }, { id: "BANK_Z", cls: "bank-z" },
];

export function BankTiles() {
  const tick = useStore((s) => s.tick);
  return (
    <div className="tiles" aria-label="Bank health">
      {BANKS.map(({ id, cls }) => {
        const b = tick?.by_bank?.[id];
        const bad = b ? pct(b.success_rate) < 95 : false;
        return (
          <div key={id} className={"tile" + (bad ? " bad" : "")}>
            <div className={"n " + cls}>{id}</div>
            <div className="v">{fmtPct(b?.success_rate)}</div>
            <div className="s mono">p95 {b ? Math.round(b.p95_ms) : "—"} ms · {b ? fmtInt(b.tps) : "—"} tps</div>
          </div>
        );
      })}
    </div>
  );
}

export function DeclineDonut() {
  const tick = useStore((s) => s.tick);
  const incident = useStore((s) => s.incident);
  const [t, b] = tick ? pctPair(tick.decline_mix.technical_pct, tick.decline_mix.business_pct) : [0, 0];
  const R = 38, C = 2 * Math.PI * R;
  const codes = incident?.decline_breakdown?.top_codes ?? [];
  const techFrac = t + b > 0 ? t / (t + b) : 0;
  return (
    <section className="panel" aria-label="Decline mix">
      <h2>Decline mix</h2>
      <div className="mixrow">
        <svg width="96" height="96" viewBox="0 0 100 100" role="img" aria-label={"Technical " + t.toFixed(0) + " percent, business " + b.toFixed(0) + " percent"}>
          <circle cx="50" cy="50" r={R} fill="none" stroke="var(--by)" strokeWidth="14" />
          <circle cx="50" cy="50" r={R} fill="none" stroke="var(--alert)" strokeWidth="14" strokeDasharray={`${C * techFrac} ${C}`} transform="rotate(-90 50 50)" style={{ transition: "stroke-dasharray .4s" }} />
          <text x="50" y="55" textAnchor="middle" fontSize="18" fontWeight="700" fill="var(--text)" fontFamily="Barlow Condensed">{t.toFixed(0)}%</text>
        </svg>
        <div style={{ flex: 1 }}>
          <div><i style={{ background: "var(--alert)" }} className="legend-i" />Technical <b className="mono">{t.toFixed(0)}%</b></div>
          <div><i style={{ background: "var(--by)" }} className="legend-i" />Business <b className="mono">{b.toFixed(0)}%</b></div>
          <div className="dim" style={{ marginTop: 4, fontSize: 12 }}>
            {codes.length ? codes.slice(0, 3).map((c) => <div key={c.code + c.pct} className="mono">{c.code} {pct(c.pct).toFixed(0)}%</div>) : "Top codes appear with an incident."}
          </div>
        </div>
      </div>
    </section>
  );
}
