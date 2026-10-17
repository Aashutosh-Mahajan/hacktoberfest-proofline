import type {
  AgentState, ActionKind, Bank, ChaosScore, CommsDraft, Deploy, EventMap, Hypothesis, Incident, MetricsTick,
  ModelStatus, ProposedAction, RCA, Series, SimStatus, VerifyCheck,
} from "@contract";

export type ConnState = "connecting" | "live" | "reconnecting" | "offline" | "mock" | "replay";

export interface ToolCallItem {
  call_id: string; tool: string; args: Record<string, unknown>;
  status: "running" | "ok" | "failed"; seq: number;
  summary?: string; evidence_ids: string[]; ms?: number; ts: number;
}
export interface ThoughtItem { id: number; seq: number; text: string; ts: number; via?: string; }
export interface VerifyAttempt { attempt: number; passed: boolean; checks: VerifyCheck[]; rejected_reason?: string; }
export interface TwinPreview {
  action_id: string; kind: ActionKind; recovers: boolean; expected_recovery_ms: number; predicted: Series; baseline: Series;
  warnings?: string[]; target_bank?: string; recovery_pct?: number; target_headroom_pct?: number;
}
export interface Proposal { action: ProposedAction; token: string; expires_ts: number; status: "pending" | "approving" | "rejected" | "executed"; }
export interface Executed { action_id: string; kind: ActionKind; predicted_recovery_ms?: number; actual_recovery_ms?: number; recovered: boolean; }
export interface Savings { actual_loss_paise: number; counterfactual_loss_paise: number; saved_paise: number; assumed_manual_mttr_ms: number; }
export interface VoiceItem { query_id: string; transcript?: string; lang?: string; answer?: string; answer_lang?: string; evidence_ids: string[]; pending: boolean; ts: number; }
export interface ChaosState { armed: boolean; round: number; revealed?: EventMap["chaos.revealed"]; scoreboard: ChaosScore[]; }
export interface FollowupItem { incident_id: string; kind: string; title: string; body: string; audience: string; simulated: boolean; }
export interface ErrorItem { id: number; code: string; message: string; recoverable: boolean; ts: number; }

export interface AppState {
  conn: ConnState;
  lastSeq: number;
  snapSeq: number;
  resyncs: number;
  contractVersion?: number;
  sim: SimStatus;
  agentState: AgentState;
  agentNote?: string;
  model: ModelStatus;
  models: string[];
  modelChoice: string | null;
  modelSwitching: boolean;
  tick?: MetricsTick;
  tickTs: number;
  deploys: Deploy[];
  incident?: Incident;
  closedIncident?: { incident_id: string; end_ts: number; mttr_ms: number };
  hypotheses: Hypothesis[];
  thoughts: ThoughtItem[];
  tools: ToolCallItem[];
  verifies: VerifyAttempt[];
  rca?: RCA;
  preview?: TwinPreview;
  proposal?: Proposal;
  executed?: Executed;
  savings?: Savings;
  totalSavedPaise: number;
  savedByIncident: Record<string, number>;
  comms?: CommsDraft[];
  commsApproved: boolean;
  followups: FollowupItem[];
  followupBusy: Record<string, boolean>;
  postmortem?: string;
  voice: VoiceItem[];
  chaos: ChaosState;
  errors: ErrorItem[];
  eventLog: { seq: number; type: string; ts: number }[];
  nextId: number;
}

export type { Bank };
