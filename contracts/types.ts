// ProofLine wire contract, version 2. SOURCE OF TRUTH shared by backend (Python dicts) and frontend.
// Every WS message is an Envelope. Money is integer paise. `ts` is simulated epoch milliseconds.
export const CONTRACT_VERSION = 2;

export type Scenario = "S0" | "S1" | "S2" | "S3" | "S5" | "S4" | "S6";
export type Bank = "BANK_X" | "BANK_Y" | "BANK_Z";
export type DeclineCode =
  | "TD_TIMEOUT" | "TD_BANK_5XX" | "TD_RATE_LIMITED"
  | "BD_WRONG_PIN" | "BD_LIMIT_EXCEEDED" | "BD_INSUFFICIENT_FUNDS";
export type HypKind = "deploy" | "bank_outage" | "traffic_spike" | "config_drift" | "business_decline";
export type HypStatus = "open" | "supported" | "ruled_out" | "confirmed";
export type ActionKind = "rollback_deploy" | "failover_bank" | "enable_circuit_breaker" | "scale_gateway" | "notify_only";
export type AgentState =
  | "idle" | "triage" | "investigating" | "concluding" | "verifying" | "previewing"
  | "proposing" | "awaiting_approval" | "remediating" | "comms" | "postmortem" | "inconclusive" | "error";

export interface Envelope<T extends string = string, D = unknown> {
  v: 2;
  seq: number;          // strictly increasing per server run; used for resume (?since=seq)
  ts: number;           // simulated ms
  type: T;
  data: D;
}

export type Series = [number, number][]; // [ts, value]

export interface DeclineBreakdown {
  technical_pct: number; business_pct: number;
  top_codes: { code: DeclineCode; pct: number }[];
}

export interface Incident {
  id: string; start_ts: number; detected_ts: number; end_ts?: number;
  baseline_success: number; current_success: number;
  decline_breakdown: DeclineBreakdown;
  blast: BlastRadius;
}
export interface BlastRadius {
  scope: string;              // e.g. "BANK_X via payment-orchestrator"
  failing_pct: number; failed_txns: number; paise_per_min_at_risk: number;
}

export interface Evidence {
  id: string;                 // e.g. "ev_deploy_482", resolves in /api/evidence/:id
  kind: "deploy" | "metric" | "log" | "bulletin" | "breakdown";
  title: string; ts?: number;
  body: Record<string, unknown> & {
    bulletin?: {
      image_url: string;
      extracted: { bank: Bank; status: string; claimed_start_ts: number; claimed_end_ts: number };
      consistent: boolean; detail: string;
    };
  };
}

export interface Hypothesis { id: string; kind: HypKind; target: string; status: HypStatus; reason: string; }

export interface VerifyCheck { name: string; passed: boolean; detail: string; }
export interface Preview {
  recovers: boolean; expected_recovery_ms: number; series: Series;
  // additive (v2.1) honest-twin fields; present on previews of failover_bank (target_* only there)
  partial?: boolean;                 // not fully recovered but materially better (e.g. target bank would overload)
  recovery_pct?: number;             // predicted final success rate as % of the pre-incident baseline
  warnings?: string[];               // plain-English caveats, e.g. "BANK_Y would run at 92% of capacity"
  target_bank?: Bank; from_bank?: Bank;
  target_headroom_pct?: { before: number; after: number } | null;   // negative = overloaded
  feasible?: boolean;                // false: no healthy bank can receive the traffic (error says why)
}
export interface ProposedAction {
  id: string; kind: ActionKind; params: Record<string, string>; label: string;
  rationale: string; preview?: Preview;
  warnings?: string[];               // additive (v2.1): copied from the preview; failover_bank params should carry from_bank + to_bank
}
export interface RCA {
  incident_id: string; cause: string; cause_label: string; confidence: number;
  evidence: { id: string; kind: Evidence["kind"]; title: string }[];
  ruled_out: { target: string; reason: string }[];
  summary: string; proposed_action?: ProposedAction; inconclusive?: boolean;
}

export interface MetricsTick {
  success_rate: number; technical_success_rate: number;
  decline_mix: { technical_pct: number; business_pct: number };
  tps: number; p95_ms: number;
  by_bank: Record<Bank, { success_rate: number; p95_ms: number; tps: number }>;
  paise_failed_per_min: number;
}

export interface CommsDraft { audience: "internal" | "merchant" | "customer"; lang: "en" | "hi"; text: string; }

export type FollowupKind = "permanent_fix" | "escalate_bank" | "merchant_notice" | "followup_tasks";
export interface FollowupDraft {      // additive (v2.1): "next steps" after an approved fix; always simulated, nothing is sent
  incident_id: string; kind: FollowupKind; title: string; body: string /* markdown */;
  audience: "Engineering" | "BANK_X" | "BANK_Y" | "BANK_Z" | "Merchants" | "Team"; simulated: true;
}

export interface ChaosScore { round: number; correct: boolean; scenario: Scenario; agent_cause: string; }

// ---- Event catalogue: type -> data ----
export interface EventMap {
  "snapshot": {
    contract_version: 2; sim: SimStatus; agent_state: AgentState;
    recent_metrics: { ts: number; m: MetricsTick }[];
    incident?: Incident; hypotheses: Hypothesis[]; rca?: RCA;
    deploys: Deploy[]; scoreboard: ChaosScore[]; model: ModelStatus; followups?: FollowupDraft[];
  };
  "sim.status": SimStatus;
  "metrics.tick": MetricsTick;
  "deploy.event": Deploy;
  "incident.opened": { incident: Incident };
  "incident.updated": { incident: Incident };
  "incident.closed": { incident_id: string; end_ts: number; mttr_ms: number };
  "agent.state": { incident_id: string; state: AgentState; note?: string };
  "agent.thought": { incident_id: string; text: string };
  "agent.tool_call": { incident_id: string; call_id: string; tool: string; args: Record<string, unknown> };
  "agent.tool_result": { incident_id: string; call_id: string; tool: string; summary: string; ok: boolean; evidence_ids: string[]; ms: number };
  "hypothesis.update": { incident_id: string; hypothesis: Hypothesis };
  "verify.result": { incident_id: string; attempt: number; passed: boolean; checks: VerifyCheck[]; rejected_reason?: string };
  "rca.ready": { rca: RCA };
  "twin.preview": { incident_id: string; action_id: string; kind: ActionKind; recovers: boolean; expected_recovery_ms: number; predicted: Series; baseline: Series;
    partial?: boolean; recovery_pct?: number; warnings?: string[]; target_bank?: Bank; from_bank?: Bank; target_headroom_pct?: { before: number; after: number } | null; feasible?: boolean };
  "followup.ready": FollowupDraft;
  "action.proposed": { incident_id: string; action: ProposedAction; approval_token: string; expires_ts: number };
  "action.executed": { incident_id: string; action_id: string; kind: ActionKind; predicted_recovery_ms?: number; actual_recovery_ms?: number; recovered: boolean };
  "impact.savings": { incident_id: string; actual_loss_paise: number; counterfactual_loss_paise: number; saved_paise: number; assumed_manual_mttr_ms: number };
  "comms.drafted": { incident_id: string; drafts: CommsDraft[] };
  "postmortem.ready": { incident_id: string; markdown: string };
  "voice.query": { query_id: string; transcript: string; lang: string };
  "voice.answer": { query_id: string; text: string; lang: string; evidence_ids: string[] };
  "chaos.armed": { round: number };
  "chaos.revealed": { round: number; scenario: Scenario; ground_truth: string; agent_cause: string; correct: boolean };
  "error": { code: string; message: string; recoverable: boolean };
}

export interface SimStatus {
  scenario: Scenario | null; running: boolean; sim_ms: number; speed: number;
  fault_active: boolean; chaos_hidden: boolean;   // when chaos_hidden, scenario is null
  replay?: boolean; replay_run_id?: string;        // additive (v2.1): true while a recorded run is being replayed (sim paused, controls ignored)
}
export interface ModelStatus { backend: "ollama" | "scripted"; name: string; healthy: boolean; warm: boolean; }
export interface Deploy { id: string; service: string; number: number; ts: number; author: string; summary: string; diff: string; }

// ---- REST (all JSON, all return {ok:boolean, error?:string, ...}) ----
// GET  /api/health               -> {ok, model: ModelStatus, contract_version}
// GET  /api/state                -> snapshot data
// GET  /api/evidence/{id}        -> Evidence
// GET  /api/bulletin/{bank}.png  -> image/png
// POST /api/scenario {scenario, seed?, severity?, fault_offset_s?}  -> {ok}
// POST /api/reset                -> {ok}
// POST /api/approve {incident_id, action_id, token} -> {ok}
// POST /api/reject  {incident_id, action_id}        -> {ok}
// POST /api/chaos                -> {ok, round}
// POST /api/ask {text, lang?}    -> {ok, query_id}      (text fallback for voice)
// POST /api/voice (multipart "audio") -> {ok, query_id}
// POST /api/followup {incident_id, kind: FollowupKind} -> {ok, error?}   result arrives as a `followup.ready` event (kept in snapshot.followups)
// POST /api/comms/approve {incident_id} -> {ok}
// GET  /api/eval/latest          -> EvalReport
// GET  /api/replay/list          -> {ok, runs: ReplayRun[], active: ReplayInfo | null}   (last 10 incident runs, newest first)
// POST /api/replay/start {run_id, speed?=1}  -> {ok, run_id, events, duration_s} | {ok:false,error}  (404 unknown run)
//        re-emits the recorded events through the same WS with original timing/speed; first message is a fresh `snapshot`,
//        sim.status carries replay:true; sim is paused, /api/sim/control & live asks return 409 until stopped.
//        The replay stays on its final frame until /api/replay/stop (or a new /api/scenario|/api/reset|/api/chaos).
// POST /api/replay/stop          -> {ok, was_active}   restores the live sim (S0 baseline)
// POST /api/model {model}        -> {ok, model: ModelStatus} | {ok:false,error}  501 if the agent cannot switch models,
//        400 unknown/not-installed model. `model` is an installed Ollama model name.
// GET  /api/health               -> also `models?: string[]` (installed Ollama models, when the agent can list them) and `replay`
// POST /api/voice (multipart "audio", optional form "duration_s") -> {ok, query_id, fallback?: "audio_unsupported", hint?}
//        audio > 30 s -> 413 {ok:false}. With fallback set, no voice.* events will follow: send the text to /api/ask.
// Validation errors on any endpoint -> 422 {ok:false, error}; the API never answers 5xx for bad input.
// WS   /ws?since=<seq>           -> first message is `snapshot`, then events with seq > since

export interface ReplayRun { run_id: string; scenario: Scenario | null; chaos: boolean; started: string; duration_s: number; events: number; }   // scenario null while an unrevealed chaos run
export interface ReplayInfo { run_id: string; playing: boolean; speed: number; position_s: number; duration_s: number; }

export interface EvalRow { scenario: Scenario; runs: number; accuracy: number; decoy_cleared_pct?: number; correctly_not_remediated_pct?: number; median_rca_s: number; p90_rca_s: number; mean_tool_calls: number; verifier_rejection_rate: number; twin_error_s: number; }
export interface EvalReport {
  generated: string; model: string; total_runs: number; false_incident_rate: number;
  rows: EvalRow[];
  ablations: { name: string; accuracy: number; runs: number }[];
}
