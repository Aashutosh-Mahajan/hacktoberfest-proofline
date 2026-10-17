import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { CommsDrafts } from "../panels/CommsDrafts";

export default function MessagesView() {
  const incident = useStore((s) => s.incident);
  const comms = useStore((s) => s.comms);
  const approved = useStore((s) => s.commsApproved);
  const markComms = useStore((s) => s.markCommsApproved);
  const hasReport = useStore((s) => !!s.postmortem);
  const setPm = useUi((u) => u.setPostmortem);
  return (
    <main className="page">
      <section className="card">
        <h2>Messages to send</h2>
        <p className="cap">Drafts for your team, merchants and customers, in English and Hindi. Nothing is sent until you approve, and even then it stays in this demo.</p>
        {comms && incident ? (
          <CommsDrafts incidentId={incident.id} drafts={comms} onApproved={markComms} />
        ) : <div className="empty"><b>No messages yet.</b>Drafts appear here after ProofLine handles a problem.</div>}
        {approved && <p className="sub">Approved. Nothing was actually sent.</p>}
      </section>
      <section className="card">
        <h2>Incident report</h2>
        <p className="cap">A short write-up of what happened, what was done, and what to do next.</p>
        {hasReport ? <button className="btn primary" onClick={() => setPm(true)}>Read the incident report</button> : <div className="empty">The report is written once the problem is fixed.</div>}
      </section>
    </main>
  );
}
