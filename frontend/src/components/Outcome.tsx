import { useEffect, useState } from "react";
import type { Evidence } from "@contract";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { api } from "../net/api";
import { confidencePct, fmtDuration, fmtPct, fmtRupees } from "../lib/format";
import { actionExplain, actionPlain, failoverBanks, fixInfo, plainify, SCENARIO_PLAIN } from "../lib/words";

/** "What went wrong": the verified cause, the proof behind it and who is affected. */
export function RcaCard() {
  const rca = useStore((s) => s.rca);
  const incident = useStore((s) => s.incident);
  const openEv = useUi((u) => u.openEvidence);
  if (!rca && !incident) return null;
  const blast = incident?.blast;
  return (
    <section className="card cause-card" aria-label="What went wrong">
      <h2><span>What went wrong</span>{rca?.inconclusive && <span className="tag supported">Not certain</span>}</h2>
      <p className="cap">The cause ProofLine found, and the proof behind it.</p>
      {rca ? (
        <>
          <h3 title={rca.cause_label}>{plainify(rca.cause_label)}</h3>
          <div className="conf" title={rca.cause}>
            <span>Confidence <b className="num">{confidencePct(rca.confidence)}%</b></span>
            <i aria-hidden="true"><b style={{ width: confidencePct(rca.confidence) + "%" }} /></i>
          </div>
          <p style={{ margin: "0 0 4px" }} title={rca.summary}>{rca.inconclusive ? "The cause has not been confirmed yet. Follow the suggested investigation steps before changing payment settings." : plainify(rca.summary)}</p>
          <div className="subhead">Proof (tap to open)</div>
          <div className="chips">{rca.evidence.map((e) => <button key={e.id} className="evchip" onClick={() => openEv(e.id)} title={e.id + " · " + e.kind}>{e.title}</button>)}</div>
          {rca.ruled_out.map((r) => <div key={r.target} className="ruledline"><b>Not the cause:</b> {r.target}. {plainify(r.reason)}</div>)}
        </>
      ) : <p className="dim" style={{ margin: 0 }}>Still looking. The answer appears here once it has been fact-checked.</p>}
      {blast && (
        <div className="affected" title="Blast radius">
          <b>Who is affected</b>
          <span>{plainify(blast.scope)}</span>
          <span><b className="num">{blast.failing_pct.toFixed(1)}%</b> of payments failing · <b className="num">{blast.failed_txns.toLocaleString("en-IN")}</b> payments so far · <b className="num">{fmtRupees(blast.paise_per_min_at_risk)}</b> lost per minute</span>
        </div>
      )}
    </section>
  );
}

/** The one thing the operator needs to do: approve (or reject) the suggested fix. */
export function ProposedActionCard() {
  const p = useStore((s) => s.proposal);
  const preview = useStore((s) => s.preview);
  const executed = useStore((s) => s.executed);
  const rca = useStore((s) => s.rca);
  const incident = useStore((s) => s.incident);
  const agentState = useStore((s) => s.agentState);
  const markProposal = useStore((s) => s.markProposal);
  const expired = useStore((s) => (p ? s.tickTs > p.expires_ts && p.status === "pending" : false));
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { setConfirming(false); }, [p?.action.id]);
  if (executed) return null;
  if (!p && !rca?.proposed_action) {
    if (!incident || (!rca && agentState !== "error")) return null;
    const rateLimited = incident.decline_breakdown.top_codes.some((c) => c.code === "TD_RATE_LIMITED" && c.pct >= 50);
    return (
      <section className="card fix" aria-label="Suggested response">
        <h2>Suggested next action</h2>
        <h3>{rateLimited ? "Check payment-processing capacity" : "Investigate the failed payments"}</h3>
        <p className="cap">No automatic fix has passed all checks. These are manual investigation steps, not an approved automatic fix.</p>
        <ol>
          <li>{rateLimited ? "Compare current request volume with available processing capacity. Check which service is rejecting requests." : "Compare failures across banks and check recent software updates."}</li>
          <li>{rateLimited ? "If our payment servers are overloaded, test adding capacity before applying the change." : "Check whether the problem is in our payment servers or at the bank or payment provider."}</li>
          <li>Check the status of pending payments before requesting another payment attempt.</li>
          <li>Notify the incident owner and affected users while monitoring recovery.</li>
        </ol>
        <p className="sub" role="note">Prepared by ProofLine's investigation checklist. A tested fix will appear for approval once the checks pass.</p>
      </section>
    );
  }
  const a = p?.action ?? rca!.proposed_action!;
  // Older runs may still contain a bank-transfer proposal. Never offer to execute it or claim recovery.
  if (a.kind === "failover_bank") return (
    <section className="card fix" aria-label="Suggested fix">
      <h2>Suggested response</h2>
      <h3>{actionPlain(a)}</h3>
      <p>{actionExplain(a, a.rationale)}</p>
      <p className="sub" role="note">Manual follow-up required. Recovery depends on the bank; no recovery time can be promised.</p>
    </section>
  );
  const incId = incident?.id ?? rca?.incident_id ?? "";
  const notify = a.kind === "notify_only";
  const outage = notify && /bank/i.test(rca?.cause ?? "");
  const noAutomaticFix = notify && !/^business[:_]/i.test(rca?.cause ?? "");
  const info = fixInfo(a as { warnings?: string[]; preview?: unknown }, preview);
  const warnings = (info.warnings ?? []).map(plainify);
  const rec = info.recovery_pct;
  const partial = !notify && preview != null && (preview.recovers === false || (rec != null && rec < 95));
  const params = Object.entries(a.params).map(([k, v]) => k + "=" + v).join("  ");
  const title = noAutomaticFix ? "No safe automatic fix" : actionPlain(a);
  const approve = async () => {
    if (!p) return;
    if (partial && !confirming) { setConfirming(true); return; }
    markProposal("approving");
    const r = await api.approve(incId, a.id, p.token);
    if (!r.ok) { markProposal("pending"); setConfirming(false); }
  };
  const bankTo = info.target_bank ?? failoverBanks(a).to;
  return (
    <section className={"card fix" + (partial ? " partial" : "")} aria-label="Suggested fix">
      <h2><span>Suggested fix</span></h2>
      <p className="cap">ProofLine only suggests. Nothing changes until you approve.</p>
      <h3 title={a.kind + (params ? " · " + params : "")}>{title}</h3>
      <p className="dim" style={{ fontSize: 14 }} title={a.rationale}>
        {outage ? "The bank itself is down, so there is nothing safe for ProofLine to switch automatically. Contact the bank and tell merchants." : noAutomaticFix ? "No automatic change passed the safety checks. Ask an engineer to investigate and notify the people affected." : actionExplain(a, a.rationale)}
      </p>
      {warnings.length > 0 && (
        <div className="caution" role="note" aria-label="Before you approve">
          <b>Before you approve</b>
          <ul>{warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </div>
      )}
      <div className="predict" aria-label="Test of the fix (simulated)" title="Digital twin preview">
        {preview ? (
          partial ? (
            <span><b className="warn-t">May not fully fix this.</b>{" "}
              {rec != null ? <>The simulated test recovers only to about <b className="num">{Math.round(rec)}%</b> of normal.</> : "The simulated test says payments may not fully recover."}</span>
          ) : preview.recovers ? (
            <><span className="num">{fmtDuration(preview.expected_recovery_ms)}</span><span>Simulated test: payments should be back to normal in about this long{rec != null ? <>, recovering to about <b className="num">{Math.round(rec)}%</b> of normal</> : ""}. The green dashed line on the chart shows it.</span></>
          ) : <span>{notify ? "This action only notifies people; it does not repair the payment system." : "The simulated test says this will not fix it."}</span>
        ) : notify ? <span>{outage ? "Contact the bank and tell merchants." : "This action only notifies people; it does not change payment settings."}</span> : <span className="dim">Testing the fix in a simulation…</span>}
      </div>
      {info.target_headroom_pct != null && info.target_headroom_pct > 0 && bankTo && <p className="sub" style={{ margin: "-4px 0 12px" }}>{bankTo} has room for about <b className="num">{Math.round(info.target_headroom_pct)}%</b> more traffic.</p>}
      {p?.status === "rejected" ? <p className="dim" style={{ margin: 0 }}>You rejected this. Nothing was changed.</p> : (
        <div className="fixactions">
          <button className={"btn big" + (partial ? "" : " primary")} disabled={!p || p.status !== "pending" || expired} onClick={approve}>
            {p?.status === "approving" ? "Fixing…" : partial ? (confirming ? "Click again to confirm" : "Approve anyway") : notify ? "Approve and notify" : "Approve fix"}
          </button>
          <button className="btn ghost" disabled={!p || p.status !== "pending"}
            onClick={async () => { if (!p) return; const r = await api.reject(incId, a.id); if (r.ok) markProposal("rejected"); }}>Reject</button>
          {!p && <span className="dim">Waiting for permission to act…</span>}
          {expired && <span style={{ color: "var(--warn)" }}>This approval expired. A new one will be sent.</span>}
        </div>
      )}
    </section>
  );
}

/** Friendly result after the fix: how fast, and how much money it saved. */
export function ResultCard() {
  const executed = useStore((s) => s.executed);
  const savings = useStore((s) => s.savings);
  const [deployed, setDeployed] = useState(false);
  useEffect(() => { setDeployed(false); }, [executed?.action_id]);
  if (!executed) return null;
  const notify = executed.kind === "notify_only";
  return (
    <section className={"card result" + (!executed.recovered && !notify ? " bad" : "")} aria-label="Result">
      <div className="hl" role="status">
        {notify ? "Teams notified"
          : executed.recovered ? <>Fixed in <span className="num">{fmtDuration(executed.actual_recovery_ms)}</span></>
          : "The fix did not recover payments"}
        {savings && <> · <span className="num">{fmtRupees(savings.saved_paise)}</span> saved</>}
      </div>
      {!notify && executed.predicted_recovery_ms != null && <p className="sub" style={{ margin: "8px 0 0" }}>The test predicted <span className="num">{fmtDuration(executed.predicted_recovery_ms)}</span>; it actually took <span className="num">{fmtDuration(executed.actual_recovery_ms)}</span>.</p>}
      {savings && <p className="sub" style={{ margin: "4px 0 0" }} title="counterfactual_loss vs actual_loss">Compared with a {Math.round(savings.assumed_manual_mttr_ms / 60000)}-minute manual response (our assumption): loss {fmtRupees(savings.actual_loss_paise)} instead of {fmtRupees(savings.counterfactual_loss_paise)}.</p>}
      {executed.recovered && !notify && (
        <div className="btnrow" style={{ marginTop: 16 }}>
          {/* UI-only confirmation; deployment is not connected to a server. */}
          {deployed ? <span role="status">Deployed to the server</span>
            : <button type="button" className="btn primary" onClick={() => setDeployed(true)}>Deploy to server</button>}
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
    <aside className="drawer" role="dialog" aria-label="Proof">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 20 }}>{ev && ev !== "loading" ? ev.title : "Proof"}</h2>
        <button className="btn" onClick={close} autoFocus>Close</button>
      </div>
      <div className="num sub">{id}</div>
      {ev === "loading" && <p>Loading…</p>}
      {ev === null && <div className="err" style={{ marginTop: 12 }}>This proof could not be loaded. The backend may not have it yet.</div>}
      {body && (
        <div style={{ display: "grid", gap: 14, marginTop: 14 }}>
          {rest.length > 0 && <table className="t"><tbody>{rest.map(([k, v]) => <tr key={k}><td className="dim">{k.replace(/_/g, " ")}</td><td className="num">{typeof v === "object" ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table>}
          {diff && <><div className="subhead" style={{ margin: 0 }}>What the update changed</div><pre className="diff">{diff.split("\n").map((l, i) => <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : ""}>{l || " "}</div>)}</pre></>}
          {lines && <><div className="subhead" style={{ margin: 0 }}>Error log lines</div><pre className="diff">{lines.map((l, i) => <div key={i} className={l.level === "ERROR" ? "d" : ""}>{l.level} {l.service}: {l.msg}</div>)}</pre></>}
          {bul && (
            <div>
              <img src={bul.image_url} alt={`Status notice from ${bul.extracted.bank}`} />
              <div className={"verify" + (bul.consistent ? "" : " amber")}>
                <b>{bul.consistent ? "The bank's notice matches our data" : "The bank's notice does not match our data"}</b>
                <div>{bul.detail}</div>
                <div className="num sub">read from the notice: {bul.extracted.bank} · {bul.extracted.status}</div>
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
        <div className="dim">Chaos round {r.round}: the hidden cause was</div>
        <div className={"mark " + (r.correct ? "ok" : "no")} aria-label={r.correct ? "Correct" : "Wrong"}>{r.correct ? "✓" : "✗"}</div>
        <div style={{ font: "700 26px/1.2 'Plus Jakarta Sans'" }}>{SCENARIO_PLAIN[r.scenario] ?? r.scenario}</div>
        <div className="sub num" style={{ marginTop: 8 }}>actual: {r.ground_truth}</div>
        <div className="sub num">AI said: {r.agent_cause}</div>
        <div className="score" style={{ margin: "16px 0" }} aria-label="Session scoreboard">{chaos.scoreboard.map((s) => <b key={s.round} className={s.correct ? "y" : "n"}>{s.correct ? "✓" : "✗"}</b>)}</div>
        <div><button className="btn primary big" onClick={() => setShown(0)} autoFocus>Continue</button></div>
      </div>
    </div>
  );
}

export function ChaosBanner() {
  const hidden = useStore((s) => s.sim.chaos_hidden);
  const round = useStore((s) => s.chaos.round);
  if (!hidden) return null;
  return <div className="hidden-banner" role="status">Chaos round {round}: the cause is hidden until ProofLine gives its answer.</div>;
}

export function ErrorState() {
  const errs = useStore((s) => s.errors);
  const dismiss = useStore((s) => s.dismissError);
  useEffect(() => {
    const timers = errs.filter((e) => e.recoverable).map((e) => window.setTimeout(() => dismiss(e.id), 10000));
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [errs, dismiss]);
  if (!errs.length) return null;
  return (
    <div className="errs" role="alert">
      {errs.slice(-3).map((e) => (
        <div key={e.id} className="err">
          <span style={{ flex: 1 }} title={e.code}>{e.message}</span>
          <button className="btn ghost" onClick={() => dismiss(e.id)}>{e.recoverable ? "Dismiss" : "Close"}</button>
        </div>
      ))}
    </div>
  );
}

export function DemoPanel({ onReconnect }: { onReconnect: () => void }) {
  const demo = useUi((u) => u.demo);
  const st = useStore();
  if (!demo) return null;
  return (
    <aside className="card demo" aria-label="Demo controls">
      <h2>Demo controls (Shift+D)</h2>
      <p className="cap num">seq {st.lastSeq} · resyncs {st.resyncs} · {st.conn} · v{st.contractVersion ?? "?"}</p>
      <div className="btnrow">
        <button className="btn" onClick={onReconnect}>Drop socket</button>
        <button className="btn" onClick={() => st.localReset()}>Clear screen</button>
        <button className="btn" onClick={() => st.pushError("demo", "Sample recoverable error", true)}>Test error</button>
      </div>
      <div className="num sub" style={{ marginTop: 12 }}>{st.eventLog.slice(-8).reverse().map((e) => <div key={e.seq}>{e.seq} {e.type}</div>)}</div>
      <div className="num sub">system success {fmtPct(st.tick?.technical_success_rate)} · system share of failures {st.tick ? st.tick.decline_mix.technical_pct.toFixed(0) : "—"}%</div>
    </aside>
  );
}
