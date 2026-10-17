import { useEffect, useState } from "react";
import { C, box, btn, btnGhost, h3, jsonCall } from "./ui";

export function ReplayControls({ onChange }: { onChange?: (playing: string | null) => void }) {
  const [runs, setRuns] = useState<string[]>([]);
  const [sel, setSel] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const load = async () => {
    const r = await jsonCall("GET", "/api/replay/list");
    if (!r.ok) { setErr(r.error ?? "cannot list replays"); return; }
    const raw = ((r as { runs?: unknown[] }).runs ?? []) as unknown[];
    const ids = raw.map((x) => (typeof x === "string" ? x : String((x as { run_id?: string }).run_id ?? (x as { id?: string }).id ?? "")));
    setRuns(ids.filter(Boolean)); setSel((s) => s || ids[0] || ""); setErr("");
  };
  useEffect(() => { void load(); }, []);
  const start = async () => {
    const r = await jsonCall("POST", "/api/replay/start", { run_id: sel });
    if (r.ok) { setPlaying(sel); onChange?.(sel); setErr(""); } else setErr(r.error ?? "start failed");
  };
  const stop = async () => {
    const r = await jsonCall("POST", "/api/replay/stop");
    if (r.ok) { setPlaying(null); onChange?.(null); setErr(""); } else setErr(r.error ?? "stop failed");
  };
  return (
    <div style={{ ...box, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }} data-testid="replay-controls">
      <h3 style={{ ...h3, margin: 0 }}>Replay</h3>
      <select value={sel} onChange={(e) => setSel(e.target.value)} disabled={!!playing}
        style={{ background: C.bg, color: C.fg, border: `1px solid ${C.border}`, borderRadius: 6, padding: "4px 6px" }}>
        {runs.length === 0 && <option value="">no recorded runs</option>}
        {runs.map((r) => <option key={r} value={r}>{r}</option>)}
      </select>
      {playing ? <button style={btn} onClick={stop}>Stop</button> : <button style={btn} disabled={!sel} onClick={start}>Play</button>}
      <button style={btnGhost} onClick={load} disabled={!!playing}>Refresh</button>
      {playing && <span style={{ color: C.ok }}>REPLAY: {playing}</span>}
      {err && <span style={{ color: C.warn }}>{err}</span>}
    </div>
  );
}
