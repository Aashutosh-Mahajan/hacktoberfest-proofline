import { useState } from "react";
import type { CommsDraft } from "@contract";
import { C, box, btn, h3 } from "./ui";
import { api } from "../net/api";

export function CommsDrafts({ incidentId, drafts, onApproved }: { incidentId: string; drafts: CommsDraft[]; onApproved?: () => void }) {
  const [lang, setLang] = useState<"en" | "hi">("en");
  const [state, setState] = useState<"idle" | "busy" | "approved" | "error">("idle");
  const [err, setErr] = useState("");
  const shown = drafts.filter((d) => d.lang === lang);
  const approve = async () => {
    setState("busy");
    const r = await api.approveComms(incidentId);
    if (r.ok) { setState("approved"); onApproved?.(); } else { setState("error"); setErr(r.error ?? "failed"); }
  };
  if (!drafts.length) return <div style={box}>No comms drafted yet.</div>;
  return (
    <div style={box} data-testid="comms-drafts">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ ...h3, margin: 0 }}>Stakeholder comms</h3>
        <div role="tablist" style={{ display: "flex", gap: 4 }}>
          {(["en", "hi"] as const).map((l) => (
            <button key={l} role="tab" aria-selected={lang === l} onClick={() => setLang(l)}
              style={{ background: lang === l ? C.accent : "transparent", color: lang === l ? "#04111f" : C.fg, border: `1px solid ${C.border}`, borderRadius: 4, padding: "2px 10px", cursor: "pointer" }}>
              {l === "en" ? "EN" : "HI"}
            </button>))}
        </div>
      </div>
      {shown.map((d) => (
        <div key={d.audience + d.lang} style={{ marginBottom: 8 }}>
          <div style={{ color: C.muted, textTransform: "uppercase", fontSize: 11, letterSpacing: 0.5 }}>{d.audience}</div>
          <div style={{ whiteSpace: "pre-wrap", background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: 8 }}>{d.text}</div>
        </div>))}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button style={{ ...btn, opacity: state === "approved" || state === "busy" ? 0.6 : 1 }} disabled={state === "approved" || state === "busy"} onClick={approve}>
          {state === "approved" ? "Approved" : "Approve comms"}
        </button>
        <span style={{ color: state === "error" ? C.warn : C.muted, fontSize: 12 }}>
          {state === "approved" ? "Marked approved (nothing is actually sent)." : state === "error" ? err : "Nothing is sent to real channels."}
        </span>
      </div>
    </div>
  );
}
