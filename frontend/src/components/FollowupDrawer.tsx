import { useEffect, useState } from "react";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { renderMarkdown } from "../panels/PostmortemModal";

const AUDIENCE: Record<string, string> = { internal: "For your team", merchant: "For merchants", customer: "For customers", bank: "For the bank", vendor: "For the bank" };

export default function FollowupDrawer() {
  const kind = useUi((u) => u.followupKind);
  const close = useUi((u) => u.closeFollowup);
  const draft = useStore((s) => (kind ? s.followups.find((f) => f.kind === kind) : undefined));
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!kind) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [kind, close]);
  useEffect(() => setCopied(false), [kind]);
  if (!kind || !draft) return null;
  const copy = async () => {
    try { await navigator.clipboard.writeText(draft.body); setCopied(true); window.setTimeout(() => setCopied(false), 2000); } catch { /* clipboard blocked */ }
  };
  return (
    <aside className="drawer" role="dialog" aria-label={draft.title}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 20, lineHeight: 1.3 }}>{draft.title}</h2>
        <button className="btn" onClick={close} autoFocus>Close</button>
      </div>
      <div style={{ margin: "10px 0 14px" }}><span className="tag">{AUDIENCE[draft.audience] ?? draft.audience}</span></div>
      <div className="md" style={{ lineHeight: 1.6 }}>{renderMarkdown(draft.body)}</div>
      <div className="fixactions" style={{ marginTop: 20 }}>
        <button className="btn primary" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <p className="sub" style={{ marginTop: 14 }} role="note">Draft only. Nothing was sent (simulated environment).</p>
    </aside>
  );
}
