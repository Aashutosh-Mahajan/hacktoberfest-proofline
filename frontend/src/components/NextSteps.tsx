import { memo } from "react";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { api } from "../net/api";

interface Step { kind: string; label: string; hint: string }

/** Follow-up work offered after a fix, chosen by what caused the incident. */
export function stepsFor(cause: string, bank?: string): Step[] {
  const tasks: Step = { kind: "followup_tasks", label: "Create follow-up tasks", hint: "A short checklist of what to do next, with owners." };
  const merchants: Step = { kind: "merchant_notice", label: "Tell merchants", hint: "A plain message for merchants about what happened." };
  if (/bank/i.test(cause)) {
    const b = bank ?? "the bank";
    return [
      { kind: "escalate_bank", label: "Report to " + b, hint: "A note for " + b + " with the timeline and evidence." },
      { kind: "permanent_fix", label: "Keep traffic on the healthy bank", hint: "How to keep payments on the healthy bank until it recovers." },
      merchants, tasks,
    ];
  }
  if (/deploy|update/i.test(cause)) {
    return [{ kind: "permanent_fix", label: "Make the fix permanent", hint: "What to change so this update cannot break payments again." }, tasks];
  }
  return [merchants, tasks];
}

export const bankFrom = (...texts: (string | undefined)[]): string | undefined => {
  for (const t of texts) { const m = t && /BANK_[A-Z]/.exec(t); if (m) return m[0]; }
  return undefined;
};

export const NextSteps = memo(function NextSteps() {
  const executed = useStore((s) => s.executed);
  const incId = useStore((s) => s.incident?.id ?? s.rca?.incident_id ?? "");
  const cause = useStore((s) => s.rca?.cause ?? "");
  const bank = useStore((s) => bankFrom(s.rca?.cause, s.proposal?.action.params?.from_bank, s.proposal?.action.params?.bank, s.incident?.blast.scope));
  const followups = useStore((s) => s.followups);
  const busy = useStore((s) => s.followupBusy);
  const setBusy = useStore((s) => s.setFollowupBusy);
  const open = useUi((u) => u.openFollowup);
  if (!executed || !incId) return null;
  const run = async (kind: string) => {
    setBusy(kind, true);
    const r = await api.followup(incId, kind);
    if (!r.ok) { setBusy(kind, false); return; }
    // some backends answer with the draft directly; otherwise followup.ready arrives on the socket
    if (typeof r.body === "string" && typeof r.title === "string") {
      useStore.getState().dispatch({ v: 2, seq: 0, ts: Date.now(), type: "followup.ready", data: { incident_id: incId, kind, audience: "internal", simulated: true, ...r } } as never);
    }
    window.setTimeout(() => useStore.getState().setFollowupBusy(kind, false), 360_000); // never spin forever
  };
  return (
    <section className="card" aria-label="Next steps">
      <h2>Next steps</h2>
      <p className="cap">Turn this fix into follow-up work. ProofLine writes the draft; you decide what to send.</p>
      <ul className="nextlist">
        {stepsFor(cause, bank).map((st) => {
          const draft = followups.find((f) => f.kind === st.kind && f.incident_id === incId) ?? followups.find((f) => f.kind === st.kind);
          const working = !!busy[st.kind] && !draft;
          return (
            <li key={st.kind}>
              <div><b>{st.label}</b><div className="sub">{st.hint}</div></div>
              {draft ? <button className="btn" onClick={() => open(st.kind)}>View draft</button>
                : <button className="btn" disabled={working} aria-busy={working} onClick={() => void run(st.kind)}>
                  {working ? <><span className="spin" aria-hidden="true" /> Gemma is writing…</> : "Draft it"}
                </button>}
            </li>
          );
        })}
      </ul>
    </section>
  );
});
