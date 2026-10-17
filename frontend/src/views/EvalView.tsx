import { EvalBoard, useEvalReport } from "../panels/EvalBoard";

export default function EvalView() {
  const { report, loading, error, reload } = useEvalReport();
  return (
    <main className="page">
      <section className="card">
        <h2>How well does ProofLine do?</h2>
        <p className="cap">Results from many simulated failures: how often it finds the right cause, and how fast.</p>
        {loading && <div className="skel" style={{ height: 160 }} />}
        {error && !loading && <div className="err">{error} <button className="btn" onClick={reload}>Try again</button></div>}
        {!loading && <EvalBoard report={report} />}
      </section>
    </main>
  );
}
