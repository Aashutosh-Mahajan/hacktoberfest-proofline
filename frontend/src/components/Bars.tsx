import { useState } from "react";
import type { Scenario } from "@contract";
import { useStore } from "../store/store";
import { api } from "../net/api";
import { useTheme } from "../lib/theme";
import { fmtPct, fmtRupees, SCENARIO_LABEL } from "../lib/format";

export type View = "command" | "eval";

const CONN: Record<string, { label: string; cls: string }> = {
  live: { label: "Live", cls: "live" }, mock: { label: "Mock feed", cls: "warn" }, replay: { label: "Replay", cls: "warn" },
  connecting: { label: "Connecting", cls: "warn" }, reconnecting: { label: "Reconnecting", cls: "warn" }, offline: { label: "Offline", cls: "bad" },
};

export function TopBar({ view, setView }: { view: View; setView: (v: View) => void }) {
  const tick = useStore((s) => s.tick);
  const conn = useStore((s) => s.conn);
  const model = useStore((s) => s.model);
  const saved = useStore((s) => s.totalSavedPaise);
  const chaos = useStore((s) => s.chaos);
  const hidden = useStore((s) => s.sim.chaos_hidden);
  const { theme, toggle } = useTheme();
  const atRisk = tick?.paise_failed_per_min ?? 0;
  const sr = tick ? tick.success_rate : NaN;
  const bad = !Number.isNaN(sr) && (sr <= 1 ? sr * 100 : sr) < 97;
  const c = CONN[conn] ?? CONN.connecting;
  const right = chaos.scoreboard.filter((r) => r.correct).length;
  return (
    <header className="top">
      <div className="brand cond">
        <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M3 17h6l3-9 5 17 3-8h9" fill="none" stroke="var(--alert)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        MAYDAY
      </div>
      <nav className="tabs" role="tablist" aria-label="Views">
        <button role="tab" aria-selected={view === "command"} onClick={() => setView("command")}>Command</button>
        <button role="tab" aria-selected={view === "eval"} onClick={() => setView("eval")}>Eval board</button>
      </nav>
      <div className="kpi"><span className="l">Success rate</span><span className={"v " + (bad ? "bad" : "good")}>{fmtPct(tick?.success_rate)}</span></div>
      <div className="kpi"><span className="l">Technical success</span><span className="v">{fmtPct(tick?.technical_success_rate)}</span></div>
      <div className="kpi"><span className="l">₹ at risk / min</span><span className={"v " + (atRisk > 0 && bad ? "bad" : "")}>{fmtRupees(atRisk)}</span></div>
      <div className="kpi"><span className="l">₹ saved</span><span className="v good">{fmtRupees(saved)}</span></div>
      <div className="spacer" />
      {chaos.scoreboard.length > 0 && (
        <span className="chip" title="Chaos scoreboard"><span className="cond">Chaos</span> <b className="mono">{right}/{chaos.scoreboard.length}</b></span>
      )}
      <span className="chip" title={model.name}><span className={"dot " + (model.healthy ? "live" : "bad")} />{model.backend === "ollama" ? "Gemma" : "Scripted"}{model.warm ? " · warm" : ""}</span>
      <span className="chip" role="status"><span className={"dot " + c.cls} />{c.label}</span>
      <button className="btn" onClick={toggle} aria-label="Toggle projector theme" aria-pressed={theme === "projector"}>{theme === "dark" ? "Projector" : "Mission"}</button>
      <ChaosButton armed={chaos.armed || hidden} />
    </header>
  );
}

function ChaosButton({ armed }: { armed: boolean }) {
  return <button className="btn primary" disabled={armed} onClick={() => api.chaos()}>{armed ? "Chaos armed" : "Chaos"}</button>;
}

const SCENARIOS: Scenario[] = ["S1", "S2", "S3", "S5", "S4", "S6", "S0"];

export function ControlBar({ onReplay }: { onReplay?: React.ReactNode }) {
  const hidden = useStore((s) => s.sim.chaos_hidden);
  const sim = useStore((s) => s.sim);
  const [sc, setSc] = useState<Scenario>("S1");
  const [sev, setSev] = useState(1);
  const [off, setOff] = useState(6);
  const [busy, setBusy] = useState(false);
  return (
    <div className="ctrl" role="toolbar" aria-label="Scenario controls">
      <label>Scenario
        <select value={sc} onChange={(e) => setSc(e.target.value as Scenario)} disabled={hidden}>
          {SCENARIOS.map((s) => <option key={s} value={s}>{hidden ? "Hidden" : s + " · " + SCENARIO_LABEL[s]}</option>)}
        </select>
      </label>
      <label>Severity <input type="range" min={0.3} max={1} step={0.1} value={sev} onChange={(e) => setSev(+e.target.value)} disabled={hidden} aria-label="Severity" /><span className="mono">{sev.toFixed(1)}</span></label>
      <label>Fault offset <input type="number" min={0} max={120} value={off} onChange={(e) => setOff(+e.target.value)} disabled={hidden} /> s</label>
      <button className="btn primary" disabled={hidden || busy} onClick={async () => { setBusy(true); await api.scenario(sc, sev, off); setBusy(false); }}>Inject fault</button>
      <button className="btn" onClick={() => api.reset()}>Reset</button>
      <span className="spacer" />
      {onReplay}
      <span className="chip mono" title="Simulated clock">
        {hidden ? "scenario hidden" : sim.scenario ? sim.scenario : "no fault"} · {sim.speed}x
      </span>
    </div>
  );
}
