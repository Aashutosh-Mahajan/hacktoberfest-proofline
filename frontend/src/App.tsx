import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useStore } from "./store/store";
import { useUi } from "./lib/ui";
import { TopBar, SimulatePopover, type View } from "./components/Bars";
import { Hero, BankTiles, WhyFailed } from "./components/Health";
import { ActivityPanel } from "./components/Agent";
import { RcaCard, ProposedActionCard, ResultCard, EvidenceDrawer, ChaosReveal, ChaosBanner, ErrorState, DemoPanel } from "./components/Outcome";

// rarely used views are split out of the initial bundle
const EvalView = lazy(() => import("./views/EvalView"));
const MessagesView = lazy(() => import("./views/MessagesView"));
const AskView = lazy(() => import("./views/AskView"));
const FollowupDrawer = lazy(() => import("./components/FollowupDrawer"));
const PostmortemModal = lazy(() => import("./panels/PostmortemModal").then((m) => ({ default: m.PostmortemModal })));

const Loading = () => <div className="page"><div className="card"><div className="skel" style={{ height: 20, width: "40%" }} /><div className="skel" style={{ height: 120, marginTop: 16 }} /></div></div>;

function PostmortemLink() {
  const has = useStore((s) => !!s.postmortem);
  const setPm = useUi((u) => u.setPostmortem);
  if (!has) return null;
  return <button className="btn cardlink" onClick={() => setPm(true)}>Read the incident report</button>;
}

function PostmortemHost() {
  const markdown = useStore((s) => s.postmortem);
  const open = useUi((u) => u.showPostmortem);
  const setPm = useUi((u) => u.setPostmortem);
  if (!markdown || !open) return null;
  return <Suspense fallback={null}><PostmortemModal markdown={markdown} open onClose={() => setPm(false)} /></Suspense>;
}

function FollowupHost() {
  const open = useUi((u) => !!u.followupKind);
  if (!open) return null;
  return <Suspense fallback={null}><FollowupDrawer /></Suspense>;
}

export function App({ onReconnect = () => {} }: { onReconnect?: () => void }) {
  const [view, setView] = useState<View>("live");
  const [simOpen, setSimOpen] = useState(false);
  const toggleDemo = useUi((u) => u.toggleDemo);
  const toggleSim = useCallback(() => setSimOpen((o) => !o), []);
  const closeSim = useCallback(() => setSimOpen(false), []);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (e.shiftKey && e.key.toLowerCase() === "d" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") toggleDemo();
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [toggleDemo]);

  return (
    <div className="app">
      <TopBar view={view} setView={setView} simOpen={simOpen} toggleSim={toggleSim} />
      {view === "live" && (
        <main className="stage">
          <div className="colv">
            <ChaosBanner />
            <Hero />
            <RcaCard />
          </div>
          <div className="colv">
            <ProposedActionCard />
            <ResultCard />
            <ActivityPanel />
            <PostmortemLink />
          </div>
        </main>
      )}
      {view === "banks" && (
        <main className="page"><BankTiles /><WhyFailed /></main>
      )}
      {view !== "live" && view !== "banks" && (
        <Suspense fallback={<Loading />}>
          {view === "messages" && <MessagesView />}
          {view === "ask" && <AskView />}
          {view === "results" && <EvalView />}
        </Suspense>
      )}
      {simOpen && <SimulatePopover onClose={closeSim} />}
      <PostmortemHost />
      <FollowupHost />
      <EvidenceDrawer />
      <ChaosReveal />
      <ErrorState />
      <DemoPanel onReconnect={onReconnect} />
    </div>
  );
}
