/** Plain-language vocabulary. The technical term always stays available as a tooltip for engineers. */
import type { ActionKind, AgentState, HypKind, HypStatus, ProposedAction } from "@contract";

export const STATE_PLAIN: Record<AgentState, string> = {
  idle: "Watching",
  triage: "Spotted the problem",
  investigating: "Looking for the cause",
  concluding: "Checking the answer",
  verifying: "Checking the answer",
  previewing: "Testing the fix",
  proposing: "Preparing a fix",
  awaiting_approval: "Waiting for your OK",
  remediating: "Fixing",
  comms: "Writing updates",
  postmortem: "Writing the report",
  inconclusive: "Could not be sure",
  error: "Something went wrong",
};

/** The seven steps shown as a progress rail. */
export const STEPS = ["Spotted", "Finding cause", "Checking", "Testing fix", "Your OK", "Fixing", "Done"] as const;

export function stepIndex(state: AgentState, recovered: boolean): number {
  switch (state) {
    case "triage": return 0;
    case "investigating": return 1;
    case "concluding": case "verifying": return 2;
    case "previewing": case "proposing": return 3;
    case "awaiting_approval": return 4;
    case "remediating": return 5;
    case "comms": case "postmortem": return 6;
    case "idle": return recovered ? 7 : -1;
    default: return -1;
  }
}

export const CODE_LABEL: Record<string, string> = {
  TD_TIMEOUT: "Bank timed out",
  TD_BANK_5XX: "Bank server error",
  TD_RATE_LIMITED: "Too many requests",
  BD_WRONG_PIN: "Wrong PIN",
  BD_LIMIT_EXCEEDED: "Limit exceeded",
  BD_INSUFFICIENT_FUNDS: "Not enough balance",
};
export const codeLabel = (c: string) => CODE_LABEL[c] ?? c;

export const HYP_KIND: Record<HypKind, string> = {
  deploy: "A recent update",
  bank_outage: "A bank problem",
  traffic_spike: "A traffic surge",
  config_drift: "A settings change",
  business_decline: "Customer-side declines",
};
export const HYP_STATUS: Record<HypStatus, string> = {
  open: "Checking",
  supported: "Looks likely",
  ruled_out: "Not the cause",
  confirmed: "The cause",
};

export const CHECK_PLAIN: Record<string, string> = {
  evidence_exists: "Proof exists",
  deploy_precedes_dip: "Update came before the drop",
  deploy_on_failing_path: "Update touches the failing path",
  scope_concentration: "Problem is concentrated in one place",
  others_healthy: "Other banks are healthy",
  no_internal_cause: "No internal cause was missed",
  traffic_elevated: "Traffic is unusually high",
  action_matches_cause: "Fix matches the cause",
  decline_class_matches: "Failure type fits the cause",
  external_claim_consistent: "Bank notice matches our data",
  twin_predicts_recovery: "Test shows payments recover",
  failover_target_healthy: "Receiving bank can safely take payments",
  cause_supported: "Cause is supported by the available data",
};
export const checkPlain = (n: string) => CHECK_PLAIN[n] ?? n.replace(/_/g, " ");

/** Explain failed checks without exposing internal rule names or raw diagnostic objects. */
export function checkFailurePlain(name: string): string {
  const reasons: Record<string, string> = {
    evidence_exists: "The explanation is missing the proof needed to support it.",
    scope_concentration: "The payment failures do not match the bank or update being blamed.",
    others_healthy: "Other banks are also having trouble, so blaming just one bank does not explain this.",
    deploy_precedes_dip: "The update has not been shown to happen before the failures began.",
    deploy_on_failing_path: "The update has not been shown to affect the payments that are failing.",
    no_internal_cause: "Recent software updates still need to be checked before accepting this explanation.",
    traffic_elevated: "The data does not show enough extra traffic to support this explanation.",
    action_matches_cause: "The suggested fix does not match the cause or targets the wrong bank or update.",
    decline_class_matches: "The types of failed payments do not support this explanation.",
    external_claim_consistent: "The bank's notice does not match the payment data.",
    twin_predicts_recovery: "The simulated test does not show enough recovery to recommend this fix.",
    failover_target_healthy: "We could not confirm that another bank can safely take these payments.",
    cause_supported: "This explanation is not one the system can verify.",
  };
  return reasons[name] ?? "This explanation did not pass a required check. More evidence is needed.";
}

export function activityPlain(text: string): string {
  if (/^Requested in parallel:/.test(text)) {
    return "Checking several sources: " + text.slice("Requested in parallel:".length).trim().split(/,\s*/).map(toolPlain).join("; ") + ".";
  }
  if (/^Triage:/.test(text)) {
    const split = /Triage: (\d+)% technical \/ (\d+)% business declines/.exec(text);
    return split ? `${split[1]}% of failed payments have system errors; ${split[2]}% involve customer limits, balances or PINs. Checking what caused the problem.` : "Checking why payments failed and who is affected.";
  }
  if (/^Concluding /.test(text)) return "Gemma has suggested an explanation. Checking it against the payment data before offering a fix.";
  return plainify(text);
}

export const TOOL_PLAIN: Record<string, string> = {
  decline_breakdown: "Checked why payments failed",
  bank_stats: "Compared the banks",
  list_deploys: "Looked at recent updates",
  get_deploy: "Read the update's changes",
  traffic: "Checked traffic volume",
  query_logs: "Read error logs",
  read_bulletin: "Read the bank's status notice",
  preview_fix: "Tested the fix",
  update_hypothesis: "Updated the possible causes",
};
export const toolPlain = (t: string) => TOOL_PLAIN[t] ?? t.replace(/_/g, " ");

/** Optional additive fields from the backend (contract v2.1): warnings, recovery %, target bank headroom. */
export interface FixExtras { warnings?: string[]; target_bank?: string; recovery_pct?: number; target_headroom_pct?: number }
type ActionLike = Pick<ProposedAction, "kind" | "params"> & { preview?: unknown };

export function failoverBanks(a: Pick<ProposedAction, "params">): { from?: string; to?: string } {
  const p = a.params ?? {};
  const bank = (v?: string) => (v && /^BANK_/.test(v) ? v : undefined);
  return { from: bank(p.from_bank) ?? bank(p.bank), to: bank(p.to_bank) ?? bank(p.target_bank) };
}

export function actionPlain(a: ActionLike): string {
  const n = a.params?.number;
  switch (a.kind as ActionKind) {
    case "rollback_deploy": return n ? `Undo the recent update (#${n})` : "Undo the recent update";
    case "failover_bank": {
      const { from } = failoverBanks(a);
      return `Contact ${from ?? "the affected bank"} and monitor recovery`;
    }
    case "enable_circuit_breaker": return "Pause traffic to the failing service";
    case "scale_gateway": return "Add capacity to the payment gateway";
    case "notify_only": return "Notify the people affected";
    default: return a.kind;
  }
}

/** One plain sentence about what the fix does (the backend's rationale stays available as a tooltip). */
export function actionExplain(a: ActionLike, rationale: string): string {
  if (a.kind === "failover_bank") {
    const { from } = failoverBanks(a);
    return `Payments using ${from ?? "the affected bank"} are failing. Raise an incident with the bank, inform affected customers and monitor recovery. These payments cannot be moved to another bank.`;
  }
  switch (a.kind) {
    case "rollback_deploy": return "Undo the software update linked to these failures and restore the previous version. The simulated test checks whether this brings payments back to normal.";
    case "scale_gateway": return "Add payment-processing capacity to handle the extra requests. The simulated test checks whether this reduces failures.";
    case "enable_circuit_breaker": return "Temporarily stop requests to the failing part of the payment system so repeated attempts do not add more pressure. This does not repair the underlying problem.";
    case "notify_only": return "Send an update to the people affected. This action does not change payment settings or repair a bank.";
    default: return plainify(rationale);
  }
}

/** Merge warnings and numbers from the proposal and the twin preview. */
export function fixInfo(action: { warnings?: string[]; preview?: unknown }, preview?: FixExtras | null): FixExtras {
  const ap = (action.preview ?? {}) as FixExtras;
  const warnings = [...new Set([...(action.warnings ?? []), ...(ap.warnings ?? []), ...(preview?.warnings ?? [])])];
  return {
    warnings,
    target_bank: preview?.target_bank ?? ap.target_bank,
    recovery_pct: preview?.recovery_pct ?? ap.recovery_pct,
    target_headroom_pct: preview?.target_headroom_pct ?? ap.target_headroom_pct,
  };
}

export const ACTION_TECH: Record<string, string> = {
  rollback_deploy: "rollback_deploy", failover_bank: "failover_bank", enable_circuit_breaker: "enable_circuit_breaker",
  scale_gateway: "scale_gateway", notify_only: "notify_only",
};

/** Cause label for the scenario names on the reveal card and the simulate menu. */
export const SCENARIO_PLAIN: Record<string, string> = {
  S0: "Everything normal",
  S1: "Bad software update",
  S2: "Bank outage",
  S3: "Bank outage with a misleading update",
  S4: "Traffic surge",
  S5: "Customers hitting limits",
  S6: "Two problems at once",
};

/** Replace decline codes (TD_TIMEOUT...) inside free text with readable labels. */
export const plainify = (text: string): string =>
  text.replace(/\b(?:TD|BD)_[A-Z0-9_]+\b/g, (c) => codeLabel(c))
    .replace(/\baddress bank:(BANK_[A-Z])/gi, "fix $1")
    .replace(/\bbank:(BANK_[A-Z])/g, "$1")
    .replace(/\bdeploy:(\d+)/g, "update #$1");
