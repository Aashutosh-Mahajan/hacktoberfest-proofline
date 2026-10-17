import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Envelope } from "@contract";
import { useStore } from "../store/store";
import { useUi } from "../lib/ui";
import { initialState, reduce } from "../store/reducer";
import { api } from "../net/api";
import { NextSteps, stepsFor } from "./NextSteps";
import FollowupDrawer from "./FollowupDrawer";

const base = () => ({
  ...initialState(), rca: undefined, incident: undefined, proposal: undefined, executed: undefined, savings: undefined, preview: undefined, tick: undefined,
});
beforeEach(() => { useStore.setState(base()); useUi.setState({ followupKind: null }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const fixed = (cause: string) => useStore.setState({
  incident: { id: "inc_1", start_ts: 1, detected_ts: 2, baseline_success: 99, current_success: 70, decline_breakdown: { technical_pct: 95, business_pct: 5, top_codes: [] }, blast: { scope: "BANK_X via payment-orchestrator", failing_pct: 1, failed_txns: 1, paise_per_min_at_risk: 1 } },
  rca: { incident_id: "inc_1", cause, cause_label: "x", confidence: 0.9, evidence: [], ruled_out: [], summary: "s" },
  executed: { action_id: "a", kind: "rollback_deploy", recovered: true },
});

describe("stepsFor", () => {
  it("offers different follow-ups by cause", () => {
    expect(stepsFor("deploy:482").map((s) => s.label)).toEqual(["Make the fix permanent", "Create follow-up tasks"]);
    expect(stepsFor("bank:BANK_X", "BANK_X").map((s) => s.label)).toEqual(["Report to BANK_X", "Keep traffic on the healthy bank", "Tell merchants", "Create follow-up tasks"]);
    expect(stepsFor("business_decline").map((s) => s.label)).toEqual(["Tell merchants", "Create follow-up tasks"]);
  });
});

describe("NextSteps", () => {
  it("is hidden until the fix has been applied", () => {
    useStore.setState({ rca: { incident_id: "inc_1", cause: "deploy:482", cause_label: "x", confidence: 1, evidence: [], ruled_out: [], summary: "" } });
    const { container } = render(<NextSteps />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a spinner while the draft is written, then View draft, and opens the draft with the honest note", async () => {
    const spy = vi.spyOn(api, "followup").mockResolvedValue({ ok: true });
    fixed("bank:BANK_X");
    render(<><NextSteps /><FollowupDrawer /></>);
    expect(screen.getByText("Report to BANK_X")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Draft it" })[0]);
    await waitFor(() => expect(spy).toHaveBeenCalledWith("inc_1", "escalate_bank"));
    expect(screen.getByText("Gemma is writing…")).toBeInTheDocument();

    // the draft arrives over the socket
    useStore.getState().dispatch({ v: 2, seq: 5, ts: 1, type: "followup.ready", data: { incident_id: "inc_1", kind: "escalate_bank", title: "Report to BANK_X", audience: "bank", simulated: true, body: "## Summary\n- 61% failed" } } as Envelope);
    const view = await screen.findByRole("button", { name: "View draft" });
    expect(screen.queryByText("Gemma is writing…")).toBeNull();
    fireEvent.click(view);
    const dlg = await screen.findByRole("dialog", { name: "Report to BANK_X" });
    expect(dlg).toHaveTextContent("For the bank");
    expect(dlg).toHaveTextContent("Summary");
    expect(dlg).toHaveTextContent("Draft only. Nothing was sent (simulated environment).");
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("stops the spinner when the request fails", async () => {
    vi.spyOn(api, "followup").mockResolvedValue({ ok: false, error: "nope" });
    fixed("deploy:482");
    render(<NextSteps />);
    fireEvent.click(screen.getAllByRole("button", { name: "Draft it" })[0]);
    await waitFor(() => expect(screen.queryByText("Gemma is writing…")).toBeNull());
    expect(screen.getAllByRole("button", { name: "Draft it" })).toHaveLength(2);
  });
});

describe("followup reducer", () => {
  it("upserts followup.ready per kind and restores followups from a snapshot", () => {
    const f = (body: string) => ({ v: 2, seq: 1, ts: 1, type: "followup.ready", data: { incident_id: "i", kind: "permanent_fix", title: "t", body, audience: "internal", simulated: true } }) as Envelope;
    let s = reduce(initialState(), f("one"));
    s = reduce(s, { ...f("two"), seq: 2 });
    expect(s.followups).toHaveLength(1);
    expect(s.followups[0].body).toBe("two");
    const snap = { v: 2, seq: 9, ts: 1, type: "snapshot", data: { contract_version: 2, sim: s.sim, agent_state: "idle", recent_metrics: [], hypotheses: [], deploys: [], scoreboard: [], model: s.model, followups: [{ incident_id: "i", kind: "followup_tasks", title: "T", body: "b", audience: "internal", simulated: true }] } } as unknown as Envelope;
    expect(reduce(s, snap).followups.map((x) => x.kind)).toEqual(["followup_tasks"]);
  });
});
