import { useEffect, useState } from "react";
import { useStore } from "./store/store";
import { useUi } from "./lib/ui";
import { TopBar, ControlBar, type View } from "./components/Bars";
import { HealthChart } from "./components/HealthChart";
import { BankTiles, DeclineDonut } from "./components/Health";
import { AgentStepper, Hypotheses, ToolStream, VerifyBadges } from "./components/Agent";
import { RcaCard, ProposedActionCard, EvidenceDrawer, ChaosReveal, ChaosBanner, ErrorState, SavingsCard, DemoPanel } from "./components/Outcome";
import { EvalBoard, useEvalReport, CommsDrafts, PostmortemModal, VoiceWarRoom, ReplayControls } from "./panels";

function EvalView() {
  const { report, loading, error, reload } = useEvalReport();
  return (
    <div className="page">
      {loading && <p className="dim">Loading eval report…</p>}
      {error && <div className="err">{error} <button className="btn" onClick={reload}>Retry</button></div>}
      <EvalBoard report={report} />
    </div>
  );
}

export function App({ onReconnect = () => {} }: { onReconnect?: () => void }) {
  const [view, setView] = useState<View>("command");
  const toggleDemo = useUi((u) => u.toggleDemo);
  const incident = useStore((s) => s.incident);
  const comms = useStore((s) => s.comms);
  const commsApproved = useStore((s) => s.commsApproved);
  const markComms = useStore((s) => s.markCommsApproved);
  const postmortem = useStore((s) => s.postmortem);
  const voice = useStore((s) => s.voice);
  const pmOpen = useUi((u) => u.showPostmortem);
  const setPm = useUi((u) => u.setPostmortem);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (e.shiftKey && e.key.toLowerCase() === "d" && tag !== "INPUT" && tag !== "TEXTAREA") toggleDemo();
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [toggleDemo]);

  return (
    <div className="app">
      <TopBar view={view} setView={setView} />
      <ControlBar onReplay={<ReplayControls />} />
      {view === "eval" ? <EvalView /> : (
        <main className="main">
          <div className="col">
            <ChaosBanner />
            <HealthChart />
            <BankTiles />
            <DeclineDonut />
            <VoiceWarRoom items={voice} />
          </div>
          <div className="col">
            <AgentStepper />
            <Hypotheses />
            <VerifyBadges />
            <ToolStream />
          </div>
          <div className="col third">
            <RcaCard />
            <ProposedActionCard />
            <SavingsCard />
            {comms && incident && (
              <CommsDrafts incidentId={incident.id} drafts={comms} onApproved={markComms} />
            )}
            {commsApproved && <div className="dim">Comms approved. Nothing was sent.</div>}
            {postmortem && <button className="btn" onClick={() => setPm(true)}>Open postmortem</button>}
          </div>
        </main>
      )}
      {postmortem && <PostmortemModal markdown={postmortem} open={pmOpen} onClose={() => setPm(false)} />}
      <EvidenceDrawer />
      <ChaosReveal />
      <ErrorState />
      <DemoPanel onReconnect={onReconnect} />
    </div>
  );
}
