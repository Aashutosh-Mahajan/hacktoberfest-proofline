import { useEffect, useRef } from "react";
import type { AgentState } from "@contract";
import { useStore } from "../store/store";
import { STATE_LABEL } from "../lib/format";

const FLOW: AgentState[] = ["triage", "investigating", "concluding", "verifying", "previewing", "proposing", "awaiting_approval", "remediating", "comms", "postmortem"];

export function AgentStepper() {
  const st = useStore((s) => s.agentState);
  const note = useStore((s) => s.agentNote);
  const idx = FLOW.indexOf(st);
  const terminal = st === "inconclusive" || st === "error";
  return (
    <section className="panel" aria-label="Agent state">
      <h2><span>Agent</span><span className="chip" role="status">{STATE_LABEL[st] ?? st}</span></h2>
      <ol className="stepper" style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {FLOW.map((f, i) => (
          <li key={f} className={"step " + (f === st ? "now" : idx > i ? "done" : "")} aria-current={f === st ? "step" : undefined}>{STATE_LABEL[f]}</li>
        ))}
        {terminal && <li className="step now">{STATE_LABEL[st]}</li>}
      </ol>
      {st === "idle" ? <p className="dim" style={{ margin: "8px 0 0" }}>All systems nominal. Proofline is watching.</p> : note ? <p className="dim" style={{ margin: "8px 0 0" }}>{note}</p> : null}
    </section>
  );
}

export function Hypotheses() {
  const hyps = useStore((s) => s.hypotheses);
  if (!hyps.length) return null;
  return (
    <section className="panel" aria-label="Hypotheses">
      <h2>Hypotheses</h2>
      <div className="hyps">
        {hyps.map((h) => (
          <div key={h.id} className={"hyp " + h.status}>
            <div className="k"><span className="mono">{h.kind}</span><span className={"badge " + h.status}>{h.status.replace("_", " ")}</span></div>
            <div className="t"><b>{h.target}</b></div>
            <div className="dim" style={{ fontSize: 12 }}>{h.reason}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function VerifyBadges() {
  const vs = useStore((s) => s.verifies);
  if (!vs.length) return null;
  return (
    <section className="panel" aria-label="Verifier">
      <h2><span>Verifier</span><span className="dim mono">{vs.filter((v) => !v.passed).length} caught</span></h2>
      {vs.map((v) => (
        <div key={v.attempt} className={"verify " + (v.passed ? "pass" : "")}>
          <b className={v.passed ? "" : "amber"}>{v.passed ? "Verified" : "Verifier rejected"}</b> <span className="dim">attempt {v.attempt}</span>
          {!v.passed && v.rejected_reason && <div>{v.rejected_reason}</div>}
          <div className="checks mono">
            {v.checks.map((c) => <span key={c.name} className={c.passed ? "" : "f"} title={c.detail}>{c.passed ? "✓" : "✗"} {c.name}</span>)}
          </div>
        </div>
      ))}
    </section>
  );
}

export function ToolStream() {
  const tools = useStore((s) => s.tools);
  const thoughts = useStore((s) => s.thoughts);
  const openEv = useUi((u) => u.openEvidence);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight; }, [tools.length, thoughts.length, tools[tools.length - 1]?.status]);
  if (!tools.length && !thoughts.length) return null;
  const merged = [
    ...tools.map((t) => ({ ts: t.ts, k: "t" as const, t })),
    ...thoughts.map((t) => ({ ts: t.ts, k: "h" as const, h: t })),
  ].sort((a, b) => a.ts - b.ts);
  return (
    <section className="panel" aria-label="Tool calls and thoughts">
      <h2>Investigation log</h2>
      <div className="scroll" ref={ref} style={{ maxHeight: 260 }} aria-live="polite">
        {merged.map((m) => m.k === "h" ? (
          <div key={"h" + m.h.id} className="thought">“{m.h.text}”</div>
        ) : (
          <div key={m.t.call_id} className="toolrow">
            {m.t.status === "running" ? <span className="spin" aria-label="running" /> : <span style={{ color: m.t.status === "ok" ? "var(--ok)" : "var(--alert)" }}>{m.t.status === "ok" ? "✓" : "✗"}</span>}
            <span className="nm mono">{m.t.tool}<span className="dim" style={{ fontWeight: 400 }}> {Object.entries(m.t.args).map(([k, v]) => k + "=" + String(v)).join(" ")}</span></span>
            <span className="dim mono">{m.t.ms != null ? m.t.ms + " ms" : ""}</span>
            {m.t.summary && <span className="sum">{m.t.summary} {m.t.evidence_ids.map((id) => <button key={id} className="evchip mono" onClick={() => openEv(id)}>{id}</button>)}</span>}
          </div>
        ))}
      </div>
    </section>
  );
}

import { useUi } from "../lib/ui";
