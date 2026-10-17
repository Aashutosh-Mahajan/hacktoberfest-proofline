import { Component, type ReactNode } from "react";

/** Last line of defence: a friendly card instead of a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {};
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { console.error("UI crashed:", error); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
        <div className="card" role="alert" style={{ maxWidth: 460, textAlign: "center" }}>
          <h2 style={{ justifyContent: "center" }}>Something went wrong</h2>
          <p className="cap">The screen hit an unexpected problem. Your system is not affected. Reload to continue.</p>
          <button className="btn primary big" onClick={() => location.reload()}>Reload</button>
          <p className="sub num" style={{ marginTop: 16 }}>{this.state.error.message}</p>
        </div>
      </div>
    );
  }
}
