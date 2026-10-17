import { useEffect, useState } from "react";
import type { EvalReport } from "@contract";
import { C, box, h3 } from "./ui";
import { api } from "../net/api";

export function useEvalReport(enabled = true): { report: EvalReport | null; loading: boolean; error?: string; reload: () => void } {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>();
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(true);
    api.evalLatest().then((r) => {
      if (!alive) return;
      if (!r) { setError("no eval report"); setReport(null); }
      else { setReport(r); setError(undefined); }
      setLoading(false);
    });
    return () => { alive = false; };
  }, [n, enabled]);
  return { report, loading, error, reload: () => setN((x) => x + 1) };
}

/** accuracy / rejection / false-incident rates are fractions; *_pct fields are already percent. */
const fr = (x: number) => Math.round(x * 100) + "%";
const pct = (x: number) => Math.round(x) + "%";
const th = { textAlign: "right" as const, padding: "4px 6px", color: C.muted, fontWeight: 500, whiteSpace: "nowrap" as const };
const td = { textAlign: "right" as const, padding: "4px 6px" };

/** Pass `report` to render directly; omit it and the component fetches /api/eval/latest itself. */
export function EvalBoard({ report }: { report?: EvalReport | null }) {
  const hook = useEvalReport(report === undefined);
  const r = report === undefined ? hook.report : report;
  if (!r) return <div style={box}>{hook.loading && report === undefined ? "Loading eval report..." : "No eval report available." + (hook.error ? " (" + hook.error + ")" : "")}</div>;
  return (
    <div style={box} data-testid="eval-board">
      <h3 style={h3}>Eval Board <span style={{ color: C.muted, fontWeight: 400 }}>{r.model} · {r.total_runs} runs · {r.generated}</span></h3>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>
            <th style={{ ...th, textAlign: "left" }}>Scenario</th><th style={th}>Runs</th><th style={th}>Accuracy</th><th style={th}>Decoy cleared</th>
            <th style={th}>Not remediated</th><th style={th}>Median RCA</th><th style={th}>p90 RCA</th><th style={th}>Tool calls</th><th style={th}>Verifier rej.</th><th style={th}>Twin err</th>
          </tr></thead>
          <tbody>{r.rows.map((x) => (
            <tr key={x.scenario} style={{ borderTop: `1px solid ${C.border}` }}>
              <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{x.scenario}</td>
              <td style={td}>{x.runs}</td>
              <td style={{ ...td, color: x.accuracy >= 0.9 ? C.ok : C.warn }}>{fr(x.accuracy)}</td>
              <td style={td}>{x.decoy_cleared_pct == null ? "–" : pct(x.decoy_cleared_pct)}</td>
              <td style={td}>{x.correctly_not_remediated_pct == null ? "–" : pct(x.correctly_not_remediated_pct)}</td>
              <td style={td}>{x.median_rca_s.toFixed(1)}s</td>
              <td style={td}>{x.p90_rca_s.toFixed(1)}s</td>
              <td style={td}>{x.mean_tool_calls.toFixed(1)}</td>
              <td style={td}>{fr(x.verifier_rejection_rate)}</td>
              <td style={td}>{x.twin_error_s.toFixed(1)}s</td>
            </tr>))}</tbody>
        </table>
      </div>
      <div style={{ margin: "8px 0", color: C.muted }}>False-incident rate (S0): <b style={{ color: r.false_incident_rate === 0 ? C.ok : C.warn }}>{fr(r.false_incident_rate)}</b></div>
      <h3 style={h3}>Ablations (accuracy)</h3>
      {r.ablations.map((a) => (
        <div key={a.name} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
          <div style={{ width: 150, color: C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={a.name}>{a.name}</div>
          <div style={{ flex: 1, background: C.bg, borderRadius: 4, height: 14 }}>
            <div style={{ width: Math.max(0, Math.min(100, a.accuracy * 100)) + "%", height: "100%", borderRadius: 4, background: a.accuracy >= 0.9 ? C.ok : C.accent }} />
          </div>
          <div style={{ width: 80, textAlign: "right" }}>{fr(a.accuracy)} <span style={{ color: C.muted }}>n={a.runs}</span></div>
        </div>
      ))}
    </div>
  );
}
