import { lazy, memo, Suspense, useEffect, useRef, useState } from "react";
import type { Scenario } from "@contract";
import { useStore } from "../store/store";
import { api } from "../net/api";
import { useTheme } from "../lib/theme";
import { SCENARIO_PLAIN } from "../lib/words";
import { describeModel, switchModel } from "../net/health";
import { useAgentBusy } from "./Agent";
import { useHealthStatus } from "./Health";

const ReplayControls = lazy(() => import("../panels/ReplayControls").then((m) => ({ default: m.ReplayControls })));

export type View = "live" | "banks" | "messages" | "ask" | "results";
const TABS: { id: View; label: string }[] = [
  { id: "live", label: "Live" }, { id: "banks", label: "Banks" }, { id: "messages", label: "Messages" }, { id: "ask", label: "Ask" }, { id: "results", label: "Results" },
];

const CONN: Record<string, string> = { connecting: "Connecting…", reconnecting: "Reconnecting…", offline: "Offline", replay: "Replay" };

export const TopBar = memo(function TopBar({ view, setView, simOpen, toggleSim }: { view: View; setView: (v: View) => void; simOpen: boolean; toggleSim: () => void }) {
  const conn = useStore((s) => s.conn);
  const chaosArmed = useStore((s) => s.chaos.armed || s.sim.chaos_hidden);
  const replaying = useStore((s) => !!s.sim.replay);
  const { theme, toggle } = useTheme();
  const status = useHealthStatus();
  return (
    <header className="top">
      <div className="brand">
        <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M3 17h6l3-9 5 17 3-8h9" fill="none" stroke="var(--accent)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        ProofLine
      </div>
      <nav className="tabs" role="tablist" aria-label="Views">
        {TABS.map((t) => <button key={t.id} role="tab" aria-selected={view === t.id} onClick={() => setView(t.id)}>{t.label}</button>)}
      </nav>
      <div className="spacer" />
      <span className={"pill " + status.kind} role="status"><i />{status.label}</span>
      <ModelSelect />
      {replaying && <span className="chip" role="status"><span className="dot warn" />Replaying a recorded run<button className="link" onClick={() => void api.replayStop()}>Stop</button></span>}
      {conn === "mock" && <span className="mockflag" role="status" title="Everything on screen comes from the in-browser mock, not a backend">MOCK DATA</span>}
      {CONN[conn] && <span className="chip" role="status"><span className="dot warn" />{CONN[conn]}</span>}
      <button className="btn" aria-expanded={simOpen} aria-controls="sim-pop" onClick={toggleSim}>Simulate</button>
      <button className="btn primary" disabled={chaosArmed} onClick={() => api.chaos()} title="Starts a hidden random failure to test ProofLine">{chaosArmed ? "Chaos armed" : "Chaos"}</button>
      <button className="btn iconbtn" onClick={toggle} aria-label="Switch to the projector theme" aria-pressed={theme === "projector"} title="Projector theme: high contrast for big screens">{theme === "dark" ? "Projector" : "Dark"}</button>
    </header>
  );
});

function ModelSelect() {
  const model = useStore((s) => s.model);
  const models = useStore((s) => s.models);
  const choice = useStore((s) => s.modelChoice);
  const switching = useStore((s) => s.modelSwitching);
  const { busy, secs } = useAgentBusy();
  const v = describeModel(model);
  const value = choice && models.includes(choice) ? choice : models.includes(model.name) ? model.name : models[0];
  const thinking = busy && v.state === "ready";
  return (
    <span className="modelsel">
      {models.length > 1 && (
        <>
          <label className="sr" htmlFor="model-select">Model</label>
          <select id="model-select" className="btn" value={value} disabled={switching} onChange={(e) => void switchModel(e.target.value)}>
            {models.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </>
      )}
      <span className="chip" role="status" title={"Active model: " + v.name} aria-label={"Model " + v.name + " " + (thinking ? "thinking " + secs + " seconds" : v.label)}>
        <span className={"dot " + (thinking ? "warn" : v.tone)} />
        {models.length <= 1 && <b className="num">{v.name}</b>}
        <span className="dim">{switching ? "switching…" : thinking ? "thinking " + secs + "s" : v.label}</span>
      </span>
    </span>
  );
}

const SCENARIOS: Scenario[] = ["S1", "S2", "S3", "S5", "S4", "S6", "S0"];

/** "Try a failure": scenario controls, chaos scoreboard and replay, tucked away until needed. */
export function SimulatePopover({ onClose }: { onClose: () => void }) {
  const hidden = useStore((s) => s.sim.chaos_hidden);
  const speed = useStore((s) => s.sim.speed);
  const scoreboard = useStore((s) => s.chaos.scoreboard);
  const [sc, setSc] = useState<Scenario>("S1");
  const [sev, setSev] = useState(1);
  const [off, setOff] = useState(6);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    ref.current?.querySelector<HTMLElement>("select")?.focus();
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const right = scoreboard.filter((r) => r.correct).length;
  return (
    <div className="card pop" id="sim-pop" role="dialog" aria-label="Simulate a failure" ref={ref}>
      <div>
        <h2>Try a failure<button className="btn ghost" onClick={onClose} aria-label="Close">Close</button></h2>
        <p className="cap" style={{ marginBottom: 0 }}>Break the simulated payment system and watch ProofLine respond.</p>
      </div>
      <label className="field">What should go wrong?
        <select value={sc} onChange={(e) => setSc(e.target.value as Scenario)} disabled={hidden}>
          {SCENARIOS.map((s) => <option key={s} value={s}>{hidden ? "Hidden" : SCENARIO_PLAIN[s]}</option>)}
        </select>
      </label>
      <label className="field row">How bad
        <input type="range" min={0.3} max={1} step={0.1} value={sev} onChange={(e) => setSev(+e.target.value)} disabled={hidden} aria-label="How bad" />
        <span className="num">{sev.toFixed(1)}</span>
      </label>
      <label className="field row">Starts after
        <input type="number" min={0} max={120} value={off} onChange={(e) => setOff(+e.target.value)} disabled={hidden} aria-label="Seconds until the failure starts" /> seconds
      </label>
      <div className="btnrow">
        <button className="btn primary" disabled={hidden || busy} onClick={async () => { setBusy(true); await api.scenario(sc, sev, off); setBusy(false); onClose(); }}>Start failure</button>
        <button className="btn" onClick={async () => { await api.reset(); onClose(); }}>Reset to healthy</button>
      </div>
      {scoreboard.length > 0 && <div className="sub">Chaos score: <b className="num">{right}/{scoreboard.length}</b> correct</div>}
      <div className="sub num">Simulation speed {speed}x</div>
      <Suspense fallback={<div className="sub">Loading replays…</div>}><ReplayControls /></Suspense>
    </div>
  );
}
