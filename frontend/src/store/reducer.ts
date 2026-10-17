import type { Envelope, EventMap, Hypothesis, MetricsTick } from "@contract";
import { pct } from "../lib/format";
import type { AppState, Series4Hz, ToolCallItem } from "./types";

export const SERIES_CAP = 960; // 4 minutes @ 4 Hz
export const LOG_CAP = 60;

const emptySeries = (): Series4Hz => ({ ts: [], overall: [], technical: [], X: [], Y: [], Z: [] });

export function initialState(): AppState {
  return {
    conn: "connecting", lastSeq: 0, resyncs: 0,
    sim: { scenario: null, running: false, sim_ms: 0, speed: 1, fault_active: false, chaos_hidden: false },
    agentState: "idle",
    model: { backend: "scripted", name: "—", healthy: false, warm: false },
    tickTs: 0,
    series: emptySeries(),
    deploys: [],
    hypotheses: [], thoughts: [], tools: [], verifies: [],
    totalSavedPaise: 0, savedByIncident: {},
    commsApproved: false,
    voice: [],
    chaos: { armed: false, round: 0, scoreboard: [] },
    errors: [], eventLog: [], nextId: 1,
  };
}

/** State that belongs to a single incident; wiped when a different incident opens or sim is reset. */
const incidentScoped = (): Partial<AppState> => ({
  incident: undefined, closedIncident: undefined, hypotheses: [], thoughts: [], tools: [], verifies: [],
  rca: undefined, preview: undefined, proposal: undefined, executed: undefined, savings: undefined,
  comms: undefined, commsApproved: false, postmortem: undefined, agentNote: undefined,
});

function pushTick(series: Series4Hz, ts: number, m: MetricsTick): Series4Hz {
  const n = series.ts.length;
  if (n && ts <= series.ts[n - 1]) {
    if (ts < series.ts[n - 1] - 5000) series = emptySeries(); // clock went backwards: new run
    else return series;
  }
  const bank = (b: "BANK_X" | "BANK_Y" | "BANK_Z") => pct(m.by_bank?.[b]?.success_rate);
  const cap = (a: number[], v: number) => (a.length >= SERIES_CAP ? [...a.slice(a.length - SERIES_CAP + 1), v] : [...a, v]);
  return {
    ts: cap(series.ts, ts), overall: cap(series.overall, pct(m.success_rate)),
    technical: cap(series.technical, pct(m.technical_success_rate)),
    X: cap(series.X, bank("BANK_X")), Y: cap(series.Y, bank("BANK_Y")), Z: cap(series.Z, bank("BANK_Z")),
  };
}

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
  // Drop already-applied duplicates (resume overlap), but never drop a snapshot.
  if (env.type !== "snapshot" && seq > 0 && seq <= state.lastSeq) return state;

  let s: AppState = { ...state, lastSeq: Math.max(state.lastSeq, seq) };
  if (env.type !== "metrics.tick" && env.type !== "snapshot") {
    s.eventLog = [...s.eventLog.slice(-(LOG_CAP - 1)), { seq, type: env.type, ts: env.ts }];
  }
  const d = env.data as never;

  switch (env.type) {
    case "snapshot": {
      const x = d as EventMap["snapshot"];
      const sameIncident = !!x.incident && x.incident.id === state.incident?.id;
      let series = emptySeries();
      for (const r of x.recent_metrics ?? []) series = pushTick(series, r.ts, r.m);
      const last = x.recent_metrics?.[x.recent_metrics.length - 1];
      s = {
        ...s,
        ...(sameIncident ? {} : incidentScoped()),
        // a snapshot may carry a lower seq than we hold (server restarted): trust it
        lastSeq: seq > 0 ? seq : state.lastSeq,
        contractVersion: x.contract_version,
        sim: x.sim, agentState: x.agent_state, model: x.model,
        series, tick: last?.m ?? state.tick, tickTs: last?.ts ?? state.tickTs,
        incident: x.incident,
        hypotheses: x.hypotheses ?? [],
        rca: x.rca ?? (sameIncident ? state.rca : undefined),
        deploys: x.deploys ?? [],
        chaos: { ...state.chaos, scoreboard: x.scoreboard ?? [], armed: state.chaos.armed && !!x.sim?.chaos_hidden },
        resyncs: state.resyncs + 1,
      };
      return s;
    }
    case "sim.status": {
      const x = d as EventMap["sim.status"];
      const rewound = state.sim.sim_ms > 0 && x.sim_ms + 5000 < state.sim.sim_ms;
      return { ...s, ...(rewound ? { ...incidentScoped(), series: emptySeries(), agentState: "idle" as const } : {}), sim: x };
    }
    case "metrics.tick": {
      const x = d as EventMap["metrics.tick"];
      return { ...s, tick: x, tickTs: env.ts, series: pushTick(s.series, env.ts, x) };
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
      return { ...s, nextId: id + 1, thoughts: [...s.thoughts.slice(-79), { id, text: x.text, ts: env.ts }] };
    }
    case "agent.tool_call": {
      const x = d as EventMap["agent.tool_call"];
      if (s.tools.some((t) => t.call_id === x.call_id)) return s;
      const item: ToolCallItem = { call_id: x.call_id, tool: x.tool, args: x.args ?? {}, status: "running", evidence_ids: [], ts: env.ts };
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
          : [...s.tools, { call_id: x.call_id, tool: x.tool, args: {}, ts: env.ts, ...patch }],
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
      return { ...s, preview: { action_id: x.action_id, kind: x.kind, recovers: x.recovers, expected_recovery_ms: x.expected_recovery_ms, predicted: x.predicted ?? [], baseline: x.baseline ?? [] } };
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
      const x = d as EventMap["error"];
      const id = s.nextId;
      return { ...s, nextId: id + 1, errors: [...s.errors.slice(-9), { id, code: x.code, message: x.message, recoverable: x.recoverable, ts: env.ts }] };
    }
    default:
      return s; // unknown event: ignore safely
  }
}
