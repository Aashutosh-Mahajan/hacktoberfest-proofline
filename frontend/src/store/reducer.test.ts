import { describe, expect, it } from "vitest";
import type { Envelope, EventMap, Incident, MetricsTick, RCA } from "@contract";
import { initialState, reduce, parseVia } from "./reducer";

let seq = 0;
const ev = <T extends keyof EventMap>(type: T, data: EventMap[T], ts = 1_000 + seq * 250): Envelope => ({ v: 2, seq: ++seq, ts, type, data }) as Envelope;

const tick = (sr = 0.99): MetricsTick => ({
  success_rate: sr, technical_success_rate: 0.995, decline_mix: { technical_pct: 30, business_pct: 70 }, tps: 800, p95_ms: 200,
  by_bank: {
    BANK_X: { success_rate: sr, p95_ms: 200, tps: 300 }, BANK_Y: { success_rate: 0.99, p95_ms: 200, tps: 300 }, BANK_Z: { success_rate: 0.99, p95_ms: 200, tps: 200 },
  }, paise_failed_per_min: 1000,
});
const inc: Incident = {
  id: "INC-1", start_ts: 1, detected_ts: 2, baseline_success: 0.99, current_success: 0.5,
  decline_breakdown: { technical_pct: 97, business_pct: 3, top_codes: [] },
  blast: { scope: "BANK_X", failing_pct: 40, failed_txns: 10, paise_per_min_at_risk: 100 },
};
const rca: RCA = { incident_id: "INC-1", cause: "deploy:482", cause_label: "x", confidence: 0.9, evidence: [], ruled_out: [], summary: "s" };

function run(events: Envelope[]) { return events.reduce(reduce, initialState()); }

describe("reducer", () => {
  it("handles snapshot", () => {
    const s = run([ev("snapshot", {
      contract_version: 2, sim: { scenario: "S1", running: true, sim_ms: 5, speed: 1, fault_active: true, chaos_hidden: false }, agent_state: "triage",
      recent_metrics: [{ ts: 1, m: tick() }, { ts: 2, m: tick() }], incident: inc, hypotheses: [], deploys: [], scoreboard: [{ round: 1, correct: true, scenario: "S1", agent_cause: "x" }],
      model: { backend: "scripted", name: "m", healthy: true, warm: true },
    })]);
    expect(s.agentState).toBe("triage");
    expect(s.incident?.id).toBe("INC-1");
    expect(s.chaos.scoreboard).toHaveLength(1);
  });

  it("covers every event type in the EventMap", () => {
    seq = 0;
    const s = run([
      ev("sim.status", { scenario: null, running: true, sim_ms: 1, speed: 1, fault_active: false, chaos_hidden: true }),
      ev("metrics.tick", tick(0.9)),
      ev("deploy.event", { id: "d1", service: "svc", number: 482, ts: 1, author: "a", summary: "s", diff: "d" }),
      ev("incident.opened", { incident: inc }),
      ev("incident.updated", { incident: { ...inc, current_success: 0.4 } }),
      ev("agent.state", { incident_id: "INC-1", state: "investigating", note: "n" }),
      ev("agent.thought", { incident_id: "INC-1", text: "hm" }),
      ev("agent.tool_call", { incident_id: "INC-1", call_id: "c1", tool: "t", args: { a: 1 } }),
      ev("agent.tool_result", { incident_id: "INC-1", call_id: "c1", tool: "t", summary: "ok", ok: true, evidence_ids: ["e1"], ms: 10 }),
      ev("hypothesis.update", { incident_id: "INC-1", hypothesis: { id: "h1", kind: "deploy", target: "x", status: "open", reason: "r" } }),
      ev("hypothesis.update", { incident_id: "INC-1", hypothesis: { id: "h1", kind: "deploy", target: "x", status: "ruled_out", reason: "r2" } }),
      ev("verify.result", { incident_id: "INC-1", attempt: 1, passed: false, checks: [{ name: "c", passed: false, detail: "d" }], rejected_reason: "bad" }),
      ev("rca.ready", { rca }),
      ev("twin.preview", { incident_id: "INC-1", action_id: "a1", kind: "rollback_deploy", recovers: true, expected_recovery_ms: 8000, predicted: [[3, 0.99]], baseline: [[3, 0.5]] }),
      ev("action.proposed", { incident_id: "INC-1", action: { id: "a1", kind: "rollback_deploy", params: {}, label: "l", rationale: "r" }, approval_token: "tok", expires_ts: 99 }),
      ev("action.executed", { incident_id: "INC-1", action_id: "a1", kind: "rollback_deploy", predicted_recovery_ms: 8000, actual_recovery_ms: 9000, recovered: true }),
      ev("impact.savings", { incident_id: "INC-1", actual_loss_paise: 1, counterfactual_loss_paise: 5, saved_paise: 4, assumed_manual_mttr_ms: 1500000 }),
      ev("comms.drafted", { incident_id: "INC-1", drafts: [{ audience: "internal", lang: "en", text: "t" }] }),
      ev("postmortem.ready", { incident_id: "INC-1", markdown: "# pm" }),
      ev("voice.query", { query_id: "q1", transcript: "hi", lang: "en" }),
      ev("voice.answer", { query_id: "q1", text: "yo", lang: "en", evidence_ids: [] }),
      ev("chaos.armed", { round: 1 }),
      ev("chaos.revealed", { round: 1, scenario: "S1", ground_truth: "deploy:482", agent_cause: "deploy:482", correct: true }),
      ev("incident.closed", { incident_id: "INC-1", end_ts: 5000, mttr_ms: 4000 }),
      ev("error", { code: "x", message: "m", recoverable: true }),
    ]);
    expect(s.sim.chaos_hidden).toBe(true);
    expect(s.deploys).toHaveLength(1);
    expect(s.incident?.current_success).toBe(0.4);
    expect(s.incident?.end_ts).toBe(5000);
    expect(s.agentState).toBe("investigating");
    expect(s.thoughts).toHaveLength(1);
    expect(s.tools[0]).toMatchObject({ status: "ok", evidence_ids: ["e1"] });
    expect(s.hypotheses).toHaveLength(1);
    expect(s.hypotheses[0].status).toBe("ruled_out");
    expect(s.verifies[0].passed).toBe(false);
    expect(s.rca?.cause).toBe("deploy:482");
    expect(s.preview?.recovers).toBe(true);
    expect(s.proposal).toMatchObject({ token: "tok", status: "executed" });
    expect(s.executed?.recovered).toBe(true);
    expect(s.totalSavedPaise).toBe(4);
    expect(s.comms).toHaveLength(1);
    expect(s.postmortem).toBe("# pm");
    expect(s.voice[0]).toMatchObject({ transcript: "hi", answer: "yo", pending: false });
    expect(s.chaos.revealed?.correct).toBe(true);
    expect(s.chaos.scoreboard).toHaveLength(1);
    expect(s.closedIncident?.mttr_ms).toBe(4000);
    expect(s.errors).toHaveLength(1);
  });

  it("ignores unknown events and duplicate seqs", () => {
    seq = 0;
    const a = reduce(initialState(), { v: 2, seq: 5, ts: 1, type: "future.thing", data: {} } as Envelope);
    expect(a.lastSeq).toBe(5);
    const b = reduce(a, ev("agent.thought", { incident_id: "x", text: "t" }, 1));
    expect(b.thoughts).toHaveLength(0); // seq 1 <= 5 is a duplicate and is dropped
    expect(reduce(a, { v: 2, seq: 3, ts: 2, type: "agent.thought", data: { incident_id: "x", text: "t" } } as Envelope).thoughts).toHaveLength(0);
  });

  it("resets incident state when a new incident opens", () => {
    seq = 0;
    const s = run([
      ev("incident.opened", { incident: inc }),
      ev("agent.thought", { incident_id: "INC-1", text: "a" }),
      ev("incident.opened", { incident: { ...inc, id: "INC-2" } }),
    ]);
    expect(s.thoughts).toHaveLength(0);
    expect(s.incident?.id).toBe("INC-2");
  });

  it("applies events replayed after a snapshot even though their seq is lower", () => {
    seq = 0;
    const snapEnv = { v: 2, seq: 50, ts: 1, type: "snapshot", data: {
      contract_version: 2, sim: { scenario: "S1", running: true, sim_ms: 9, speed: 1, fault_active: true, chaos_hidden: false }, agent_state: "awaiting_approval",
      recent_metrics: [], incident: inc, hypotheses: [], deploys: [], scoreboard: [], model: { backend: "ollama", name: "gemma4:e4b", healthy: true, warm: true },
      pending_action: { incident_id: "INC-1", action: { id: "a1", kind: "rollback_deploy", params: {}, label: "l", rationale: "r" }, approval_token: "tok", expires_ts: 9e12 },
    } } as unknown as Envelope;
    const replay = (n: number, type: string, data: unknown): Envelope => ({ v: 2, seq: n, ts: n, type, data }) as Envelope;
    const s = [
      snapEnv,
      replay(10, "agent.thought", { incident_id: "INC-1", text: "Checked deploys via gemma4:e4b" }),
      replay(11, "agent.tool_call", { incident_id: "INC-1", call_id: "c1", tool: "bank_stats", args: {} }),
      replay(12, "metrics.tick", tick()),
      replay(51, "agent.thought", { incident_id: "INC-1", text: "live thought" }),
    ].reduce(reduce, initialState());
    expect(s.thoughts.map((t) => t.text)).toEqual(["Checked deploys", "live thought"]);
    expect(s.thoughts[0].via).toBe("gemma4:e4b");
    expect(s.tools).toHaveLength(1);
    expect(s.proposal?.token).toBe("tok"); // Approve survives a reconnect
    expect(s.tick).toBeUndefined(); // replayed ticks are ignored; the snapshot carries history
  });

  it("fully resets on a new snapshot (scenario change / reset)", () => {
    seq = 0;
    const s1 = run([ev("incident.opened", { incident: inc }), ev("agent.thought", { incident_id: "INC-1", text: "old" }), ev("rca.ready", { rca })]);
    const fresh = { v: 2, seq: 1, ts: 5, type: "snapshot", data: {
      contract_version: 2, sim: { scenario: null, running: true, sim_ms: 1, speed: 1, fault_active: false, chaos_hidden: false }, agent_state: "idle",
      recent_metrics: [], hypotheses: [], deploys: [], scoreboard: [], model: { backend: "ollama", name: "m", healthy: true, warm: true },
    } } as unknown as Envelope;
    const s2 = reduce(s1, fresh);
    expect(s2.incident).toBeUndefined();
    expect(s2.thoughts).toHaveLength(0);
    expect(s2.rca).toBeUndefined();
    expect(s2.agentState).toBe("idle");
  });

  it("does not re-toast replayed errors", () => {
    const snapEnv = { v: 2, seq: 20, ts: 1, type: "snapshot", data: { contract_version: 2, sim: { scenario: null, running: true, sim_ms: 1, speed: 1, fault_active: false, chaos_hidden: false }, agent_state: "idle", recent_metrics: [], hypotheses: [], deploys: [], scoreboard: [], model: { backend: "ollama", name: "m", healthy: true, warm: true } } } as unknown as Envelope;
    const s = [snapEnv, { v: 2, seq: 3, ts: 1, type: "error", data: { code: "x", message: "old", recoverable: true } } as Envelope].reduce(reduce, initialState());
    expect(s.errors).toHaveLength(0);
  });
});

describe("parseVia", () => {
  it("splits the model badge off thought text", () => {
    expect(parseVia("Checking deploys via gemma4:e4b")).toEqual({ text: "Checking deploys", via: "gemma4:e4b" });
    expect(parseVia("scope BANK_X via payment-orchestrator")).toEqual({ text: "scope BANK_X via payment-orchestrator" });
  });
});
