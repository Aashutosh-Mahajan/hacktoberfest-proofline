import { useEffect, useState } from "react";
import type { Evidence } from "@contract";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { api } from "../net/api";
import { ACTION_LABEL, SCENARIO_LABEL, confidencePct, fmtDuration, fmtPct, fmtRupees, pct } from "../lib/format";

export function RcaCard() {
  const rca = useStore((s) => s.rca);
  const incident = useStore((s) => s.incident);
  const openEv = useUi((u) => u.openEvidence);
  if (!rca && !incident) return (
    <section className="panel"><h2>Root cause</h2><p className="dim" style={{ margin: 0 }}>No incident. The RCA card appears here once the verifier passes.</p></section>
  );
  const blast = incident?.blast;
  return (
    <section className="panel rca" aria-label="Root cause analysis">
      <h2><span>Root cause</span>{rca?.inconclusive && <span className="badge supported">Inconclusive</span>}</h2>
      {rca ? (
        <>
          <h3>{rca.cause_label}</h3>
          <div className="dim mono" style={{ fontSize: 12 }}>{rca.cause} · confidence {confidencePct(rca.confidence)}%</div>
          <div className="conf" aria-hidden><i style={{ width: confidencePct(rca.confidence) + "%" }} /></div>
          <p style={{ margin: "4px 0" }}>{rca.summary}</p>
          <div className="chips">{rca.evidence.map((e) => <button key={e.id} className="evchip" onClick={() => openEv(e.id)} title={e.id}><span className="dim">{e.kind}</span> {e.title}</button>)}</div>
          {rca.ruled_out.map((r) => <div key={r.target} className="ruled">Ruled out <b>{r.target}</b>: {r.reason}</div>)}
        </>
      ) : <p className="dim" style={{ margin: "0 0 6px" }}>Investigating. Verified cause will appear here.</p>}
      {blast && (
        <div className="mono" style={{ marginTop: 8, fontSize: 12, borderTop: "1px solid var(--line)", paddingTop: 6 }}>
          Blast radius: {blast.scope} · {blast.failing_pct.toFixed(1)}% failing · {blast.failed_txns.toLocaleString("en-IN")} txns · {fmtRupees(blast.paise_per_min_at_risk)}/min
        </div>
      )}
    </section>
  );
}

export function ProposedActionCard() {
  const p = useStore((s) => s.proposal);
  const preview = useStore((s) => s.preview);
  const executed = useStore((s) => s.executed);
  const rca = useStore((s) => s.rca);
  const incident = useStore((s) => s.incident);
  const savings = useStore((s) => s.savings);
  const markProposal = useStore((s) => s.markProposal);
  const simMs = useStore((s) => s.tickTs);
  if (!p && !rca?.proposed_action) return null;
  const a = p?.action ?? rca!.proposed_action!;
  const incId = incident?.id ?? rca?.incident_id ?? "";
  const expired = p ? simMs > p.expires_ts && p.status === "pending" : false;
  const notify = a.kind === "notify_only";
  const rec = executed;
  return (
    <section className="panel" aria-label="Proposed action" style={{ borderColor: p?.status === "pending" ? "var(--warn)" : undefined }}>
      <h2><span>Proposed action</span><span className="badge open">{ACTION_LABEL[a.kind] ?? a.kind}</span></h2>
      <div style={{ font: "700 19px 'Barlow Condensed'" }}>{a.label}</div>
      <p style={{ margin: "2px 0 6px" }}>{a.rationale}</p>
      <div className="mono dim" style={{ fontSize: 12 }}>{Object.entries(a.params).map(([k, v]) => k + "=" + v).join("  ")}</div>
      <div style={{ margin: "8px 0", padding: 8, borderRadius: 4, background: "var(--panel2)" }} aria-label="Fix preview">
        <div className="cond dim" style={{ fontSize: 12 }}>Fix preview (digital twin)</div>
        {preview ? (
          preview.recovers
            ? <div>Predicted back to normal in <b className="mono">{fmtDuration(preview.expected_recovery_ms)}</b>. The dashed green line on the chart is the forecast.</div>
            : <div>{notify ? "No technical fix to preview: infrastructure is healthy, so Proofline recommends comms." : "Twin predicts this action will not recover. Do not approve."}</div>
        ) : <div className="dim">Waiting for the twin…</div>}
      </div>
      {rec ? (
        <div className="verify pass" role="status">
          <b>{notify ? "Notifications queued" : rec.recovered ? "Recovered" : "Not recovered"}</b>{" "}
          {!notify && <span className="mono">predicted {fmtDuration(rec.predicted_recovery_ms)} · actual {fmtDuration(rec.actual_recovery_ms)}</span>}
          {savings && <div>{fmtRupees(savings.saved_paise)} loss avoided vs a {Math.round(savings.assumed_manual_mttr_ms / 60000)} min manual response (assumption).</div>}
        </div>
      ) : p?.status === "rejected" ? <div className="dim">Rejected. Nothing was executed.</div> : (
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn primary" disabled={!p || p.status !== "pending" || expired}
            onClick={async () => { if (!p) return; markProposal("approving"); const r = await api.approve(incId, a.id, p.token); if (!r.ok) markProposal("pending"); }}>
            {p?.status === "approving" ? "Approving…" : "Approve"}
          </button>
          <button className="btn" disabled={!p || p.status !== "pending"}
            onClick={async () => { if (!p) return; const r = await api.reject(incId, a.id); if (r.ok) markProposal("rejected"); }}>Reject</button>
          {!p && <span className="dim">Approval token not received yet.</span>}
          {expired && <span style={{ color: "var(--warn)" }}>Token expired.</span>}
        </div>
      )}
    </section>
  );
}

export function EvidenceDrawer() {
  const id = useUi((u) => u.evidenceId);
  const close = useUi((u) => u.closeEvidence);
  const [ev, setEv] = useState<Evidence | null | "loading">("loading");
  useEffect(() => {
    if (!id) return;
    let live = true;
    setEv("loading");
    api.evidence(id).then((e) => live && setEv(e));
    const k = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", k);
    return () => { live = false; window.removeEventListener("keydown", k); };
  }, [id, close]);
  if (!id) return null;
  const body = ev && ev !== "loading" ? ev.body : null;
  const bul = body?.bulletin;
  const rest = body ? Object.entries(body).filter(([k]) => k !== "bulletin" && k !== "diff" && k !== "lines") : [];
  const diff = typeof body?.diff === "string" ? (body.diff as string) : null;
  const lines = Array.isArray(body?.lines) ? (body!.lines as { ts?: number; service?: string; level?: string; msg?: string }[]) : null;
  return (
    <aside className="drawer" role="dialog" aria-label="Evidence">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 className="cond" style={{ margin: 0 }}>{ev && ev !== "loading" ? ev.title : id}</h2>
        <button className="btn" onClick={close} autoFocus>Close</button>
      </div>
      <div className="mono dim">{id}</div>
      {ev === "loading" && <p>Loading evidence…</p>}
      {ev === null && <div className="err" style={{ marginTop: 10 }}>Evidence {id} could not be loaded. The backend may not have it yet.</div>}
      {body && (
        <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
          {rest.length > 0 && <table className="t"><tbody>{rest.map(([k, v]) => <tr key={k}><td className="dim">{k}</td><td className="mono" style={{ textAlign: "left" }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table>}
          {diff && <pre className="diff mono">{diff.split("\n").map((l, i) => <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : ""}>{l || " "}</div>)}</pre>}
          {lines && <pre className="diff mono">{lines.map((l, i) => <div key={i} className={l.level === "ERROR" ? "d" : ""}>{l.level} {l.service}: {l.msg}</div>)}</pre>}
          {bul && (
            <div>
              <img src={bul.image_url} alt={`Bulletin from ${bul.extracted.bank}`} />
              <div className="verify pass" style={bul.consistent ? undefined : { borderColor: "var(--alert)" }}>
                <b className={bul.consistent ? "" : "amber"}>{bul.consistent ? "Consistent with telemetry" : "Contradicts telemetry"}</b>
                <div>{bul.detail}</div>
                <div className="mono dim">extracted: {bul.extracted.bank} · {bul.extracted.status}</div>
              </div>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

export function ChaosReveal() {
  const chaos = useStore((s) => s.chaos);
  const [shown, setShown] = useState<number>(0);
  const r = chaos.revealed;
  useEffect(() => { if (r) setShown(r.round); }, [r]);
  if (!r || shown !== r.round) return null;
  return (
    <div className="reveal" role="alertdialog" aria-label="Chaos reveal">
      <div className="card">
        <div className="cond dim">Chaos round {r.round} · ground truth</div>
        <div className={"mark " + (r.correct ? "ok" : "no")} aria-label={r.correct ? "Correct" : "Wrong"}>{r.correct ? "✓" : "✗"}</div>
        <div style={{ font: "700 26px 'Barlow Condensed'" }}>{SCENARIO_LABEL[r.scenario] ?? r.scenario}</div>
        <div className="mono">truth: {r.ground_truth}</div>
        <div className="mono">agent: {r.agent_cause}</div>
        <div className="score" style={{ margin: "12px 0" }} aria-label="Session scoreboard">{chaos.scoreboard.map((s) => <b key={s.round} className={s.correct ? "y" : "n"}>{s.correct ? "✓" : "✗"}</b>)}</div>
        <div><button className="btn primary" onClick={() => setShown(0)} autoFocus>Continue</button></div>
      </div>
    </div>
  );
}

export function ChaosBanner() {
  const hidden = useStore((s) => s.sim.chaos_hidden);
  const round = useStore((s) => s.chaos.round);
  if (!hidden) return null;
  return <div className="hidden-banner" role="status">Chaos round {round}: fault hidden. Scenario name is revealed after the agent answers.</div>;
}

export function ErrorState() {
  const errs = useStore((s) => s.errors);
  const dismiss = useStore((s) => s.dismissError);
  if (!errs.length) return null;
  return (
    <div className="errs" role="alert">
      {errs.slice(-3).map((e) => (
        <div key={e.id} className="err">
          <b>{e.code}</b><span style={{ flex: 1 }}>{e.message}</span>
          <button className="btn ghost" onClick={() => dismiss(e.id)}>{e.recoverable ? "Dismiss" : "Close"}</button>
        </div>
      ))}
    </div>
  );
}

export function SavingsCard() {
  const s = useStore((x) => x.savings);
  if (!s) return null;
  return (
    <section className="panel" aria-label="Rupees saved">
      <h2>Impact</h2>
      <div style={{ font: "700 34px/1 'Barlow Condensed'", color: "var(--ok)" }}>{fmtRupees(s.saved_paise)} saved</div>
      <div className="dim mono" style={{ fontSize: 12 }}>
        Actual loss {fmtRupees(s.actual_loss_paise)} vs {fmtRupees(s.counterfactual_loss_paise)} if the fault ran {Math.round(s.assumed_manual_mttr_ms / 60000)} min (assumed manual response)
      </div>
    </section>
  );
}

export function DemoPanel({ onReconnect }: { onReconnect: () => void }) {
  const demo = useUi((u) => u.demo);
  const st = useStore();
  if (!demo) return null;
  return (
    <aside className="panel demo" aria-label="Demo controls">
      <h2>Demo controls (Shift+D)</h2>
      <div className="mono" style={{ fontSize: 12 }}>seq {st.lastSeq} · resyncs {st.resyncs} · conn {st.conn} · v{st.contractVersion ?? "?"}</div>
      <div style={{ display: "flex", gap: 6, margin: "6px 0", flexWrap: "wrap" }}>
        <button className="btn" onClick={onReconnect}>Drop socket</button>
        <button className="btn" onClick={() => st.localReset()}>Clear UI</button>
        <button className="btn" onClick={() => st.pushError("demo", "Sample recoverable error", true)}>Test error</button>
      </div>
      <div className="mono dim" style={{ fontSize: 11 }}>{st.eventLog.slice(-10).reverse().map((e) => <div key={e.seq}>{e.seq} {e.type}</div>)}</div>
      <div className="mono dim" style={{ fontSize: 11 }}>tech {fmtPct(st.tick?.technical_success_rate)} · mix {st.tick ? pct(st.tick.decline_mix.technical_pct).toFixed(0) : "—"}%</div>
    </aside>
  );
}
