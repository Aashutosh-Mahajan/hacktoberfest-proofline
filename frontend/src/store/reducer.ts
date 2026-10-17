import type { Envelope, EventMap, Hypothesis, ProposedAction } from "@contract";
import type { AppState, FollowupItem, ToolCallItem } from "./types";

/** If thought text carries "via gemma4:e4b" (or "via scripted"), split it into a badge. Optional. */
export function parseVia(text: string): { text: string; via?: string } {
  const m = /\s*[(\[]?\bvia (gemma[\w:.\-]*|scripted)\b[)\]]?/i.exec(text);
  if (!m) return { text };
  return { text: (text.slice(0, m.index) + text.slice(m.index + m[0].length)).replace(/\s+([.,;:])/g, "$1").trim(), via: m[1].toLowerCase() };
}

export const LOG_CAP = 60;


export function initialState(): AppState {
  return {
    conn: "connecting", lastSeq: 0, snapSeq: 0, resyncs: 0,
    sim: { scenario: null, running: false, sim_ms: 0, speed: 1, fault_active: false, chaos_hidden: false },
    agentState: "idle",
    model: { backend: "ollama", name: "—", healthy: false, warm: false }, models: [], modelChoice: null, modelSwitching: false,
    tickTs: 0,
    deploys: [],
    hypotheses: [], thoughts: [], tools: [], verifies: [],
    totalSavedPaise: 0, savedByIncident: {},
    commsApproved: false, followups: [], followupBusy: {},
    voice: [],
    chaos: { armed: false, round: 0, scoreboard: [] },
    errors: [], eventLog: [], nextId: 1,
  };
}

/** State that belongs to a single incident; wiped when a different incident opens or sim is reset. */
const incidentScoped = (): Partial<AppState> => ({
  incident: undefined, closedIncident: undefined, hypotheses: [], thoughts: [], tools: [], verifies: [],
  rca: undefined, preview: undefined, proposal: undefined, executed: undefined, savings: undefined,
  comms: undefined, commsApproved: false, followups: [], followupBusy: {}, postmortem: undefined, agentNote: undefined,
});

function upsertHyp(list: Hypothesis[], h: Hypothesis): Hypothesis[] {
  const i = list.findIndex((x) => x.id === h.id);
  if (i < 0) return [...list, h];
  const next = list.slice();
  next[i] = h;
  return next;
}

function upsertDeploy(list: AppState["deploys"], d: AppState["deploys"][number]) {
  return list.some((x) => x.id === d.id) ? list.map((x) => (x.id === d.id ? d : x)) : [...list, d];
}

const sumSaved = (m: Record<string, number>) => Object.values(m).reduce((a, b) => a + b, 0);

/** Pure reducer: apply one envelope. Unknown event types are ignored (but seq still advances). */
export function reduce(state: AppState, env: Envelope): AppState {
  if (!env || typeof env.type !== "string") return state;
  const seq = typeof env.seq === "number" ? env.seq : state.lastSeq;
  // Drop already-applied duplicates, but never drop a snapshot. After a snapshot lastSeq is reset to 0 so the
  // replayed events (seq <= snapshot seq) that follow it are applied in order.
  if (env.type !== "snapshot" && seq > 0 && seq <= state.lastSeq) return state;
  // the bus ring can contain older snapshots; after the head snapshot of this connection they must not reset us again
  if (env.type === "snapshot" && state.snapSeq > 0 && seq <= state.snapSeq) return state;
  const replaying = env.type !== "snapshot" && state.snapSeq > 0 && seq <= state.snapSeq;

  let s: AppState = { ...state, lastSeq: Math.max(state.lastSeq, seq) };
  if (env.type !== "metrics.tick" && env.type !== "snapshot") {
    s.eventLog = [...s.eventLog.slice(-(LOG_CAP - 1)), { seq, type: env.type, ts: env.ts }];
  }
  const d = env.data as never;

  switch (env.type) {
    case "snapshot": {
      const x = d as EventMap["snapshot"];
      const last = x.recent_metrics?.[x.recent_metrics.length - 1];
      const p = (x as { pending_action?: { incident_id?: string; action: ProposedAction; approval_token: string; expires_ts: number } }).pending_action;
      // Full reset: the sim clock may have jumped back and the bus ring was cleared. Everything incident-scoped
      // (thoughts, tools, savings, comms...) is rebuilt from the events replayed after the snapshot.
      s = {
        ...initialState(),
        conn: state.conn, resyncs: state.resyncs + 1, errors: state.errors, nextId: state.nextId, voice: [],
        eventLog: s.eventLog, lastSeq: 0, snapSeq: seq,
        contractVersion: x.contract_version,
        sim: x.sim, agentState: x.agent_state, model: x.model, models: state.models, modelChoice: state.modelChoice, modelSwitching: state.modelSwitching,
        tick: last?.m, tickTs: last?.ts ?? 0,
        incident: x.incident,
        hypotheses: x.hypotheses ?? [],
        rca: x.rca,
        deploys: x.deploys ?? [],
        followups: ((x as { followups?: FollowupItem[] }).followups ?? []).filter((f) => f && f.kind),
        proposal: p ? { action: p.action, token: p.approval_token, expires_ts: p.expires_ts, status: "pending" } : undefined,
        chaos: { armed: !!x.sim?.chaos_hidden, round: state.chaos.round, scoreboard: x.scoreboard ?? [] },
      };
      return s;
    }
    case "sim.status": {
      const x = d as EventMap["sim.status"];
      if (replaying) return s; // the snapshot already carries the latest sim status
      const rewound = state.sim.sim_ms > 0 && x.sim_ms + 5000 < state.sim.sim_ms;
      return { ...s, ...(rewound ? { ...incidentScoped(), agentState: "idle" as const } : {}), sim: x };
    }
    case "metrics.tick": {
      if (replaying) return s; // snapshot.recent_metrics covers history
      const x = d as EventMap["metrics.tick"];
      return { ...s, tick: x, tickTs: env.ts };
    }
    case "deploy.event":
      return { ...s, deploys: upsertDeploy(s.deploys, d as EventMap["deploy.event"]) };
    case "incident.opened": {
      const x = d as EventMap["incident.opened"];
      const fresh = x.incident.id !== state.incident?.id;
      return { ...s, ...(fresh ? incidentScoped() : {}), incident: x.incident };
    }
    case "incident.updated":
      return { ...s, incident: (d as EventMap["incident.updated"]).incident };
    case "incident.closed": {
      const x = d as EventMap["incident.closed"];
      return {
        ...s, closedIncident: x,
        incident: s.incident && s.incident.id === x.incident_id ? { ...s.incident, end_ts: x.end_ts } : s.incident,
      };
    }
    case "agent.state": {
      const x = d as EventMap["agent.state"];
      return { ...s, agentState: x.state, agentNote: x.note };
    }
    case "agent.thought": {
      const x = d as EventMap["agent.thought"];
      const id = s.nextId;
      const v = parseVia(x.text);
      return { ...s, nextId: id + 1, thoughts: [...s.thoughts.slice(-79), { id, seq, text: v.text, via: v.via, ts: env.ts }] };
    }
    case "agent.tool_call": {
      const x = d as EventMap["agent.tool_call"];
      if (s.tools.some((t) => t.call_id === x.call_id)) return s;
      const item: ToolCallItem = { call_id: x.call_id, tool: x.tool, args: x.args ?? {}, status: "running", evidence_ids: [], ts: env.ts, seq };
      return { ...s, tools: [...s.tools, item] };
    }
    case "agent.tool_result": {
      const x = d as EventMap["agent.tool_result"];
      const patch = { status: (x.ok ? "ok" : "failed") as ToolCallItem["status"], summary: x.summary, evidence_ids: x.evidence_ids ?? [], ms: x.ms };
      const exists = s.tools.some((t) => t.call_id === x.call_id);
      return {
        ...s,
        tools: exists
          ? s.tools.map((t) => (t.call_id === x.call_id ? { ...t, ...patch } : t))
          : [...s.tools, { call_id: x.call_id, tool: x.tool, args: {}, ts: env.ts, seq, ...patch }],
      };
    }
    case "hypothesis.update":
      return { ...s, hypotheses: upsertHyp(s.hypotheses, (d as EventMap["hypothesis.update"]).hypothesis) };
    case "verify.result": {
      const x = d as EventMap["verify.result"];
      const v = { attempt: x.attempt, passed: x.passed, checks: x.checks ?? [], rejected_reason: x.rejected_reason };
      const rest = s.verifies.filter((a) => a.attempt !== x.attempt);
      return { ...s, verifies: [...rest, v].sort((a, b) => a.attempt - b.attempt) };
    }
    case "rca.ready":
      return { ...s, rca: (d as EventMap["rca.ready"]).rca };
    case "twin.preview": {
      const x = d as EventMap["twin.preview"];
      const extra = x as typeof x & { warnings?: string[]; target_bank?: string; recovery_pct?: number; target_headroom_pct?: number };
      return { ...s, preview: { action_id: x.action_id, kind: x.kind, recovers: x.recovers, expected_recovery_ms: x.expected_recovery_ms, predicted: x.predicted ?? [], baseline: x.baseline ?? [], warnings: extra.warnings, target_bank: extra.target_bank, recovery_pct: extra.recovery_pct, target_headroom_pct: extra.target_headroom_pct } };
    }
    case "action.proposed": {
      const x = d as EventMap["action.proposed"];
      return { ...s, proposal: { action: x.action, token: x.approval_token, expires_ts: x.expires_ts, status: "pending" }, executed: undefined };
    }
    case "action.executed": {
      const x = d as EventMap["action.executed"];
      return {
        ...s,
        executed: { action_id: x.action_id, kind: x.kind, predicted_recovery_ms: x.predicted_recovery_ms, actual_recovery_ms: x.actual_recovery_ms, recovered: x.recovered },
        proposal: s.proposal ? { ...s.proposal, status: "executed" } : s.proposal,
      };
    }
    case "impact.savings": {
      const x = d as EventMap["impact.savings"];
      const savedByIncident = { ...s.savedByIncident, [x.incident_id]: x.saved_paise };
      return {
        ...s, savedByIncident, totalSavedPaise: sumSaved(savedByIncident),
        savings: { actual_loss_paise: x.actual_loss_paise, counterfactual_loss_paise: x.counterfactual_loss_paise, saved_paise: x.saved_paise, assumed_manual_mttr_ms: x.assumed_manual_mttr_ms },
      };
    }
    case "comms.drafted":
      return { ...s, comms: (d as EventMap["comms.drafted"]).drafts ?? [], commsApproved: false };
    case "followup.ready" as never: {
      const f = d as unknown as FollowupItem;
      if (!f || !f.kind) return s;
      const rest = s.followups.filter((x) => !(x.kind === f.kind && x.incident_id === f.incident_id));
      return { ...s, followups: [...rest, f], followupBusy: { ...s.followupBusy, [f.kind]: false } };
    }
    case "postmortem.ready":
      return { ...s, postmortem: (d as EventMap["postmortem.ready"]).markdown };
    case "voice.query": {
      const x = d as EventMap["voice.query"];
      const i = s.voice.findIndex((v) => v.query_id === x.query_id);
      const base = i >= 0 ? s.voice[i] : { query_id: x.query_id, evidence_ids: [], pending: true, ts: env.ts };
      const item = { ...base, transcript: x.transcript, lang: x.lang };
      return { ...s, voice: i >= 0 ? s.voice.map((v, j) => (j === i ? item : v)) : [...s.voice, item] };
    }
    case "voice.answer": {
      const x = d as EventMap["voice.answer"];
      const i = s.voice.findIndex((v) => v.query_id === x.query_id);
      const base = i >= 0 ? s.voice[i] : { query_id: x.query_id, evidence_ids: [], pending: true, ts: env.ts };
      const item = { ...base, answer: x.text, answer_lang: x.lang, evidence_ids: x.evidence_ids ?? [], pending: false };
      return { ...s, voice: i >= 0 ? s.voice.map((v, j) => (j === i ? item : v)) : [...s.voice, item] };
    }
    case "chaos.armed": {
      const x = d as EventMap["chaos.armed"];
      return { ...s, chaos: { ...s.chaos, armed: true, round: x.round, revealed: undefined } };
    }
    case "chaos.revealed": {
      const x = d as EventMap["chaos.revealed"];
      const score = { round: x.round, correct: x.correct, scenario: x.scenario, agent_cause: x.agent_cause };
      const scoreboard = [...s.chaos.scoreboard.filter((r) => r.round !== x.round), score];
      return { ...s, chaos: { armed: false, round: x.round, revealed: x, scoreboard } };
    }
    case "error": {
      if (replaying) return s; // historical errors must not re-toast after a reconnect
      const x = d as EventMap["error"];
      const id = s.nextId;
      return { ...s, nextId: id + 1, errors: [...s.errors.slice(-9), { id, code: x.code, message: x.message, recoverable: x.recoverable, ts: env.ts }] };
    }
    default:
      return s; // unknown event: ignore safely
  }
}
