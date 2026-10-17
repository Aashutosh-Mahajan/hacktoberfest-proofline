import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Hypothesis, ProposedAction, RCA } from "@contract";
import { useStore } from "../store/store";
import { initialState } from "../store/reducer";
import { RcaCard, ProposedActionCard, ResultCard, ChaosReveal } from "./Outcome";
import { Hypotheses, VerifyBadges } from "./Agent";
import { api } from "../net/api";

beforeEach(() => useStore.setState({ ...initialState(), rca: undefined, incident: undefined, proposal: undefined, executed: undefined, savings: undefined, preview: undefined, tick: undefined }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const rca: RCA = {
  incident_id: "inc_1", cause: "deploy:482", cause_label: "Bad deploy #482 on payment-orchestrator", confidence: 0.93,
  evidence: [{ id: "ev_deploy_482", kind: "deploy", title: "Deploy #482" }],
  ruled_out: [{ target: "bank:BANK_X", reason: "healthy" }], summary: "The update landed 4s before the dip.",
};
const action: ProposedAction = { id: "act_1", kind: "rollback_deploy", params: { number: "482" }, label: "Roll back #482", rationale: "Undo it." };

describe("RcaCard", () => {
  it("shows the plain-language cause, proof and who is affected", () => {
    useStore.setState({
      rca,
      incident: { id: "inc_1", start_ts: 1, detected_ts: 2, baseline_success: 99, current_success: 70, decline_breakdown: { technical_pct: 95, business_pct: 5, top_codes: [] }, blast: { scope: "BANK_X", failing_pct: 29.7, failed_txns: 889, paise_per_min_at_risk: 1_000_000 } },
    });
    render(<RcaCard />);
    expect(screen.getByText("Bad deploy #482 on payment-orchestrator")).toBeInTheDocument();
    expect(screen.getByText(/Confidence/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy #482" })).toBeInTheDocument();
    expect(screen.getByText("Who is affected")).toBeInTheDocument();
    expect(screen.getByText(/Not the cause:/)).toBeInTheDocument();
  });
  it("renders nothing without an incident", () => {
    const { container } = render(<RcaCard />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ProposedActionCard", () => {
  it("provides concrete next steps when surge diagnosis cannot be verified", () => {
    useStore.setState({ agentState: "inconclusive", rca: { ...rca, cause: "inconclusive", inconclusive: true },
      incident: { id: "inc_1", start_ts: 1, detected_ts: 2, baseline_success: 99, current_success: 50,
        decline_breakdown: { technical_pct: 99, business_pct: 1, top_codes: [{ code: "TD_RATE_LIMITED", pct: 98 }] },
        blast: { scope: "all banks", failing_pct: 49, failed_txns: 100, paise_per_min_at_risk: 1000 } } });
    render(<ProposedActionCard />);
    expect(screen.getByRole("heading", { name: "Check payment-processing capacity" })).toBeInTheDocument();
    expect(screen.getByText(/Check the status of pending payments/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve fix" })).toBeNull();
    expect(screen.getByRole("note")).toHaveTextContent("investigation checklist");
  });
  it("explains the fix, shows the predicted recovery and approves with the token", () => {
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ ok: true });
    useStore.setState({
      rca, proposal: { action, token: "tok", expires_ts: 9e12, status: "pending" },
      preview: { action_id: "act_1", kind: "rollback_deploy", recovers: true, expected_recovery_ms: 6000, predicted: [], baseline: [] },
    });
    render(<ProposedActionCard />);
    expect(screen.getByText("Undo the recent update (#482)")).toBeInTheDocument();
    expect(screen.getByText(/Simulated test/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve fix" }));
    expect(screen.getByRole("button", { name: "Fixing…" })).toBeDisabled(); // optimistic
    expect(approve).toHaveBeenCalledWith("inc_1", "act_1", "tok");
  });
  it("disables approval when the token has expired", () => {
    useStore.setState({ rca, tickTs: 10, proposal: { action, token: "tok", expires_ts: 5, status: "pending" } });
    render(<ProposedActionCard />);
    expect(screen.getByRole("button", { name: "Approve fix" })).toBeDisabled();
  });
  it("hides after execution while ResultCard celebrates", () => {
    useStore.setState({
      rca, proposal: { action, token: "t", expires_ts: 9e12, status: "executed" },
      executed: { action_id: "act_1", kind: "rollback_deploy", predicted_recovery_ms: 6000, actual_recovery_ms: 9000, recovered: true },
      savings: { actual_loss_paise: 1, counterfactual_loss_paise: 2, saved_paise: 34_000_000, assumed_manual_mttr_ms: 1_500_000 },
    });
    const { container } = render(<><ProposedActionCard /><ResultCard /></>);
    expect(screen.queryByRole("button", { name: "Approve fix" })).toBeNull();
    expect(container.textContent).toContain("Fixed in");
    expect(container.textContent).toContain("saved");
  });
});

describe("Hypotheses (possible causes)", () => {
  it("explains a shared outage plainly and keeps raw rejection diagnostics in details", () => {
    const diagnostic = "others_healthy: other banks degraded: {'BANK_X': -49.1, 'BANK_Z': -48.1}";
    useStore.setState({ verifies: [{ attempt: 1, passed: false, rejected_reason: diagnostic,
      checks: [{ name: "others_healthy", passed: false, detail: diagnostic }] }] });
    const { container, rerender } = render(<VerifyBadges />);
    expect(screen.getByText(/Other banks are also having trouble/)).toBeInTheDocument();
    expect(container.textContent).not.toContain("others_healthy");
    expect(container.textContent).not.toContain("-49.1");
    rerender(<VerifyBadges showChecks />);
    expect(screen.getByText(/Technical details:/)).toHaveTextContent(diagnostic);
  });
  it("uses plain words and keeps the technical kind as a tooltip", () => {
    const hyps: Hypothesis[] = [
      { id: "h1", kind: "deploy", target: "deploy:482", status: "confirmed", reason: "Landed 4s before the dip" },
      { id: "h2", kind: "bank_outage", target: "bank:BANK_X", status: "ruled_out", reason: "Bank is healthy" },
    ];
    useStore.setState({ hypotheses: hyps });
    render(<Hypotheses />);
    expect(screen.getByText("The cause")).toBeInTheDocument();
    expect(screen.getByText("Not the cause")).toBeInTheDocument();
    expect(screen.getByText("A recent update").closest("[title]")).toHaveAttribute("title", "deploy · deploy:482");
  });
  it("fact check shows what it caught", () => {
    useStore.setState({ verifies: [
      { attempt: 1, passed: false, rejected_reason: "Deploy came after the dip", checks: [{ name: "deploy_precedes_dip", passed: false, detail: "x" }] },
      { attempt: 2, passed: true, checks: [{ name: "evidence_exists", passed: true, detail: "ok" }] },
    ] });
    render(<VerifyBadges showChecks />);
    expect(screen.getByText(/caught a weak answer/)).toBeInTheDocument();
    expect(screen.getByText(/Fact check passed/)).toBeInTheDocument();
    expect(screen.getByText(/Update came before the drop/)).toBeInTheDocument();
  });
});

describe("Chaos reveal", () => {
  it("shows the verdict and scoreboard, and dismisses", () => {
    useStore.setState({ chaos: { armed: false, round: 2, scoreboard: [{ round: 1, correct: true, scenario: "S1", agent_cause: "x" }, { round: 2, correct: false, scenario: "S3", agent_cause: "y" }], revealed: { round: 2, scenario: "S3", ground_truth: "bank outage", agent_cause: "deploy:483", correct: false } } });
    render(<ChaosReveal />);
    expect(screen.getByLabelText("Wrong")).toBeInTheDocument();
    expect(screen.getByText("Bank outage with a misleading update")).toBeInTheDocument();
    expect(screen.getByLabelText("Session scoreboard").children).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});

describe("ProposedActionCard: failover and caution cases", () => {
  const failover: ProposedAction & { warnings: string[] } = {
    id: "act_f", kind: "failover_bank", params: { from_bank: "BANK_X", to_bank: "BANK_Y" }, label: "Fail over BANK_X traffic to BANK_Y",
    rationale: "Fail over to address bank:BANK_X", warnings: ["BANK_Y would run at 92% of capacity", "This works around the problem; BANK_X is still down"],
  };
  const pv = (over: Record<string, unknown> = {}) => ({ action_id: "act_f", kind: "failover_bank" as const, recovers: true, expected_recovery_ms: 4000, predicted: [], baseline: [], recovery_pct: 97, target_bank: "BANK_Y", target_headroom_pct: 8, ...over });

  it("names both banks, lists warnings and shows recovery and headroom", () => {
    useStore.setState({ rca: { ...rca, cause: "bank:BANK_X" }, proposal: { action: failover, token: "t", expires_ts: 9e12, status: "pending" }, preview: pv() });
    const { container } = render(<ProposedActionCard />);
    expect(screen.getByText("Contact BANK_X and monitor recovery")).toBeInTheDocument();
    expect(container.textContent).not.toContain("address bank:");
    expect(container.textContent).toContain("These payments cannot be moved to another bank");
    expect(container.textContent).not.toContain("BANK_Y");
    expect(container.textContent).not.toContain("recovering to about");
    expect(screen.queryByRole("button", { name: "Approve fix" })).toBeNull();
  });

  it("demotes approval to confirm-twice when the test may not fully fix it", () => {
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ ok: true });
    useStore.setState({ rca: { ...rca, cause: "bank:BANK_X" }, proposal: { action: failover, token: "t", expires_ts: 9e12, status: "pending" }, preview: pv({ recovers: false, recovery_pct: 78 }) });
    render(<ProposedActionCard />);
    expect(screen.getByText(/Recovery depends on the bank/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve anyway" })).toBeNull();
    expect(approve).not.toHaveBeenCalled();
  });

  it("says there is no safe automatic fix for a bank outage with notify_only", () => {
    const notify: ProposedAction = { id: "n", kind: "notify_only", params: {}, label: "Notify", rationale: "r" };
    useStore.setState({ rca: { ...rca, cause: "bank:BANK_X" }, proposal: { action: notify, token: "t", expires_ts: 9e12, status: "pending" } });
    const { container } = render(<ProposedActionCard />);
    expect(screen.getByText("No safe automatic fix")).toBeInTheDocument();
    expect(container.textContent).toContain("Contact the bank and tell merchants");
  });
});

