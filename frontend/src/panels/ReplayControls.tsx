import { useEffect, useState } from "react";
import type { ReplayRun } from "@contract";
import { C, box, btn, btnGhost, h3 } from "./ui";
import { api } from "../net/api";
import { useStore } from "../store/store";
import { SCENARIO_PLAIN } from "../lib/words";

export const runLabel = (r: Partial<ReplayRun> & { run_id: string; label?: string }) =>
  r.label ?? [r.chaos && r.scenario == null ? "Hidden (chaos)" : r.scenario ? SCENARIO_PLAIN[r.scenario] ?? r.scenario : null,
    r.duration_s != null ? Math.round(r.duration_s) + " s" : null].filter(Boolean).join(" · ") + " · " + r.run_id.slice(-6);

export function ReplayControls({ onChange }: { onChange?: (playing: string | null) => void }) {
  const [runs, setRuns] = useState<(Partial<ReplayRun> & { run_id: string })[]>([]);
  const [sel, setSel] = useState("");
  const playing = useStore((s) => (s.sim.replay ? s.sim.replay_run_id ?? "replay" : null));
  const load = async () => {
    const r = await api.replayList();
    if (!r.ok) return;
    const raw = ((r as { runs?: unknown[] }).runs ?? []) as unknown[];
    const list = raw.map((x) => (typeof x === "string" ? { run_id: x } : (x as ReplayRun))).filter((x) => x.run_id);
    setRuns(list); setSel((s) => (list.some((x) => x.run_id === s) ? s : list[0]?.run_id || ""));
  };
  useEffect(() => { void load(); }, []);
  const start = async () => { const r = await api.replayStart(sel); if (r.ok) onChange?.(sel); };
  const stop = async () => { const r = await api.replayStop(); if (r.ok) onChange?.(null); };
  return (
    <div style={{ ...box, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", padding: "8px 10px" }} data-testid="replay-controls">
      <h3 style={{ ...h3, margin: 0 }}>Replay</h3>
      <select aria-label="Recorded run" value={sel} onChange={(e) => setSel(e.target.value)} disabled={!!playing}
        style={{ background: C.bg, color: C.fg, border: `1px solid ${C.border}`, borderRadius: 8, padding: "4px 6px", maxWidth: 220 }}>
        {runs.length === 0 && <option value="">no recorded runs yet</option>}
        {runs.map((r) => <option key={r.run_id} value={r.run_id}>{runLabel(r)}</option>)}
      </select>
      {playing ? <button style={btn} onClick={stop}>Stop replay</button> : <button style={btn} disabled={!sel} onClick={start}>Play</button>}
      <button style={btnGhost} onClick={load} disabled={!!playing}>Refresh</button>
    </div>
  );
}
