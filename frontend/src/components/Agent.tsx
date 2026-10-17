import { useEffect, useRef, useState } from "react";
import type { AgentState } from "@contract";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { STATE_PLAIN, STEPS, plainify, activityPlain, checkFailurePlain, stepIndex, HYP_KIND, HYP_STATUS, checkPlain, toolPlain } from "../lib/words";

const ACTIVE: AgentState[] = ["triage", "investigating", "concluding", "verifying", "previewing", "proposing", "remediating", "comms", "postmortem"];

/** Wall-clock seconds since `key` last changed. Ticks every second while running. */
function useElapsed(key: string, running: boolean): number {
  const [t0, setT0] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { setT0(Date.now()); setNow(Date.now()); }, [key]);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);
  return Math.max(0, Math.floor((now - t0) / 1000));
}

/** Agent busy state + seconds since the last agent update (shared by the panel and the top bar). */
export function useAgentBusy() {
  const st = useStore((s) => s.agentState);
  const incident = useStore((s) => s.incident);
  const faultActive = useStore((s) => s.sim.fault_active);
  const nTools = useStore((s) => s.tools.length);
  const nThoughts = useStore((s) => s.thoughts.length);
  const active = ACTIVE.includes(st);
  const detecting = st === "idle" && faultActive && !incident;
  const secs = useElapsed(st + "|" + nTools + "|" + nThoughts + "|" + (detecting ? "d" : ""), active || detecting);
  return { busy: active, detecting, secs };
}

const NON_MODEL: Partial<Record<AgentState, string>> = {
  verifying: "Fact-checking the answer with fixed rules…",
  previewing: "Testing the fix in a simulation…",
  proposing: "Preparing the fix for your approval…",
  remediating: "Applying the approved fix and watching recovery…",
};

/** Live progress so a slow model call (minutes on CPU) never looks frozen. */
export function ThinkingIndicator() {
  const { busy, detecting, secs } = useAgentBusy();
  const st = useStore((s) => s.agentState);
  const runningTool = useStore((s) => s.tools.find((t) => t.status === "running"));
  if (!busy && !detecting) return null;
  const what = detecting
    ? "Watching for a lasting drop (about 10 seconds below normal)…"
    : runningTool ? toolPlain(runningTool.tool) + "…"
    : NON_MODEL[st] ?? "Gemma is thinking (CPU)… this can take a few minutes.";
  return (
    <div className="think" role="status" aria-live="polite">
      <span className="spin" aria-hidden="true" />
      <span>{what}</span>
      <span className="num" title="Seconds since the last update">{secs}s{secs >= 20 ? " since last update" : ""}</span>
    </div>
  );
}

export function Progress() {
  const raw = useStore((s) => s.agentState);
  const recovered = useStore((s) => !!s.executed?.recovered);
  const opened = useStore((s) => !!s.incident && !s.incident.end_ts);
  const st: AgentState = raw === "idle" && opened && !recovered ? "triage" : raw; // show progress the instant an incident opens
  const note = useStore((s) => s.agentNote);
  const idx = stepIndex(st, recovered);
  const terminal = st === "inconclusive" || st === "error";
  const label = idx === 7 ? "Done" : STATE_PLAIN[st] ?? st;
  return (
    <div>
      <ol className="rail" aria-label="Progress">
        {STEPS.map((name, i) => <li key={name} title={name} className={i < idx ? "done" : i === idx ? "now" : ""} aria-current={i === idx ? "step" : undefined} />)}
      </ol>
      <div className="railnow">
        <b role="status" title={st}>{terminal ? STATE_PLAIN[st] : label}</b>
        {note && st !== "idle" && <span className="sub" title={note}>{terminal ? "No fix has been approved by the checks. Review the evidence or ask an engineer to investigate." : plainify(note)}</span>}
      </div>
    </div>
  );
}

export function Hypotheses() {
  const hyps = useStore((s) => s.hypotheses);
  if (!hyps.length) return null;
  return (
    <div aria-label="Possible causes">
      <div className="subhead">Possible causes</div>
      <div className="causes">
        {hyps.map((h) => (
          <div key={h.id} className={"cause " + h.status}>
            <span className="t" title={h.kind + " · " + h.target}>{HYP_KIND[h.kind] ?? h.kind}{/^bank:|^deploy:/.test(h.target) && <span className="dim" style={{ fontWeight: 400 }}> · {plainify(h.target)}</span>}</span>
            <span className={"tag " + h.status} title={h.status}>{HYP_STATUS[h.status]}</span>
            <span className="r">{plainify(h.reason)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function VerifyBadges({ showChecks = false }: { showChecks?: boolean }) {
  const vs = useStore((s) => s.verifies);
  if (!vs.length) return null;
  const last = vs[vs.length - 1];
  const caught = vs.filter((v) => !v.passed);
  return (
    <div aria-label="Fact check">
      <div className="subhead" title="Deterministic verifier">Fact check</div>
      {caught.map((v) => (
        <div key={v.attempt} className="fact caught">
          <span><b className="caught">Fact check caught a weak answer</b> and sent it back (try {v.attempt}).</span>
          <span>{[...new Set(v.checks.filter((c) => !c.passed).map((c) => checkFailurePlain(c.name)))].join(" ") || "The explanation needs more evidence before a fix can be offered."}</span>
          {showChecks && v.rejected_reason && <p className="sub">Technical details: {v.rejected_reason}</p>}
          {showChecks && <div className="checks">{v.checks.map((c) => <span key={c.name} className={c.passed ? "" : "f"} title={c.detail}>{c.passed ? "✓" : "✗"} {checkPlain(c.name)}</span>)}</div>}
        </div>
      ))}
      {last.passed && (
        <div className="fact">
          <span><b className="ok">Fact check passed.</b> {last.checks.filter((c) => c.passed).length} of {last.checks.length} checks agree with the answer.</span>
          {showChecks && <div className="checks">{last.checks.map((c) => <span key={c.name} title={c.detail}>✓ {checkPlain(c.name)}</span>)}</div>}
        </div>
      )}
    </div>
  );
}

export function ToolStream() {
  const tools = useStore((s) => s.tools);
  const openEv = useUi((u) => u.openEvidence);
  if (!tools.length) return <p className="dim">No tool calls yet.</p>;
  return (
    <div aria-label="Tool calls">
      {tools.map((t) => (
        <div key={t.call_id} className="toolrow">
          {t.status === "running" ? <span className="spin" aria-label="running" /> : <span className={t.status === "ok" ? "ok-t" : "bad-t"}>{t.status === "ok" ? "✓" : "✗"}</span>}
          <span title={t.tool + " " + Object.entries(t.args).map(([k, v]) => k + "=" + String(v)).join(" ")}>{toolPlain(t.tool)}</span>
          <span className="num dim">{t.ms != null ? t.ms + " ms" : ""}</span>
          {t.summary && <span className="sum">{t.summary} {t.evidence_ids.map((id) => <button key={id} className="evchip num" onClick={() => openEv(id)}>{id}</button>)}</span>}
        </div>
      ))}
    </div>
  );
}

/** One column: progress, plain summary lines, possible causes and fact check. Tool-call detail is behind a toggle. */
export function ActivityPanel() {
  const thoughts = useStore((s) => s.thoughts);
  const st = useStore((s) => s.agentState);
  const incident = useStore((s) => s.incident);
  const faultActive = useStore((s) => s.sim.fault_active);
  const [details, setDetails] = useState(false);
  const ref = useRef<HTMLUListElement>(null);
  useEffect(() => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight; }, [thoughts.length]);
  const quiet = st === "idle" && !incident && !faultActive && !thoughts.length;
  const connecting = useStore((s) => s.conn === "connecting" && s.snapSeq === 0);
  return (
    <section className="card" aria-label="What the AI is doing">
      <h2>What the AI is doing</h2>
      <p className="cap">ProofLine's work, step by step, in plain words.</p>
      {connecting ? (
        <div className="skel" style={{ height: 96 }} aria-label="Loading" />
      ) : quiet ? (
        <div className="empty"><b>Nothing to fix right now.</b>Everything looks healthy. ProofLine is watching.</div>
      ) : (
        <>
          <Progress />
          <ThinkingIndicator />
          {thoughts.length > 0 && (
            <ul className="steps" ref={ref} aria-live="polite" aria-label="Summary of steps">
              {thoughts.map((t) => (
                <li key={t.id} title={t.text}>{activityPlain(t.text)}{t.via && <span className="via" title={"Written by " + t.via}>{t.via}</span>}</li>
              ))}
            </ul>
          )}
          <Hypotheses />
          <VerifyBadges showChecks={details} />
          {(thoughts.length > 0 || incident) && (
            <div style={{ marginTop: 16 }}>
              <button className="link" aria-expanded={details} onClick={() => setDetails((d) => !d)}>{details ? "Hide details" : "Show details"}</button>
              {details && <div style={{ marginTop: 8 }}><div className="subhead">Everything it looked at</div><ToolStream /></div>}
            </div>
          )}
        </>
      )}
    </section>
  );
}
