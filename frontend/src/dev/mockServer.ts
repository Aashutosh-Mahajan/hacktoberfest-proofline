/**
 * In-browser event simulator (?mock=1). Plays realistic S1 / S2 / S3 / S5 sequences, including a verifier rejection,
 * twin preview, approval flow, recovery, comms, postmortem, voice answers and Chaos Mode, so the UI never
 * depends on the Python backend. Emits contract-v2 envelopes only. Rates go out as fractions (0..1) on purpose;
 * the UI normalises both fractions and percents.
 */
import type {
  ActionKind, Bank, Deploy, Envelope, EvalReport, Evidence, EventMap, Hypothesis, Incident, MetricsTick, Scenario,
} from "@contract";
import type { Transport } from "../net/api";

type Emit = (env: Envelope) => void;
type Kind = "S1" | "S2" | "S3" | "S5";
interface Job { at: number; fn: () => void }
interface Fault { kind: Kind; start: number; severity: number; recoverAt: number | null; rampMs: number }

const EPOCH = Date.UTC(2026, 9, 17, 9, 30, 0);
const SHARE: Record<Bank, number> = { BANK_X: 0.4, BANK_Y: 0.35, BANK_Z: 0.25 };
const HEALTHY = 99.2;

const noise = (t: number, k: number) => Math.sin(t / 700 + k * 11.7) * 0.18 + Math.sin(t / 230 + k * 3.1) * 0.12;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

function bulletinSvg(bank: string, status: string, from: string, to: string): string {
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'>" +
    "<rect width='640' height='360' fill='#f5f1e6'/><rect x='0' y='0' width='640' height='56' fill='#1d2a44'/>" +
    "<text x='24' y='36' font-family='Arial' font-size='22' font-weight='700' fill='#fff'>" + bank + " - SERVICE BULLETIN</text>" +
    "<text x='24' y='104' font-family='Arial' font-size='15' fill='#333'>Status: " + status + "</text>" +
    "<text x='24' y='136' font-family='Arial' font-size='15' fill='#333'>Window: " + from + " to " + to + " IST</text>" +
    "<text x='24' y='168' font-family='Arial' font-size='15' fill='#333'>Impact: UPI collect and pay requests may time out.</text>" +
    "<text x='24' y='200' font-family='Arial' font-size='15' fill='#333'>Engineers are working on restoration.</text>" +
    "<rect x='24' y='290' width='240' height='36' fill='none' stroke='#b33' stroke-width='2'/>" +
    "<text x='36' y='314' font-family='Arial' font-size='14' fill='#b33'>SYNTHETIC - NOT A REAL BANK</text></svg>";
  return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

const DIFF_482 = [
  "--- a/orchestrator/config/routing.yaml",
  "+++ b/orchestrator/config/routing.yaml",
  "@@ -41,7 +41,7 @@ banks:",
  "   BANK_X:",
  "     adapter: bank-adapter-x",
  "-    timeout_ms: 3000",
  "+    timeout_ms: 800",
  "     retries: 1",
  "   BANK_Y:",
  "     timeout_ms: 3000",
].join("\n");

export class MockServer {
  private seq = 0;
  private simMs = EPOCH;
  private speed: number;
  private timer?: number;
  private jobs: Job[] = [];
  private fault: Fault | null = null;
  private scenario: Scenario | null = null;
  private chaosHidden = false;
  private chaosRound = 0;
  private chaosTruth: { scenario: Scenario; truth: string } | null = null;
  private incidentN = 2040;
  private incident: Incident | null = null;
  private agentState: EventMap["agent.state"]["state"] = "idle";
  private pending: { incident_id: string; action_id: string; token: string; kind: ActionKind; params: Record<string, string>; chosen: Kind } | null = null;
  private deploys: Deploy[] = [{ id: "d_481", service: "wallet", number: 481, ts: EPOCH - 3_600_000, author: "r.iyer", summary: "wallet: bump pool size", diff: "-pool: 20\n+pool: 24" }];
  private scoreboard: EventMap["snapshot"]["scoreboard"] = [];
  private queryN = 0;
  private callN = 0;
  private history: { ts: number; m: MetricsTick }[] = [];

  constructor(private emitRaw: Emit, opts: { speed?: number } = {}) {
    this.speed = opts.speed ?? 1;
  }

  // ---- lifecycle ---------------------------------------------------------------------------------
  start() {
    for (let i = 480; i > 0; i--) {
      const t = this.simMs - i * 250;
      this.history.push({ ts: t, m: this.metricsAt(t) });
    }
    this.emitSnapshot();
    this.timer = window.setInterval(() => this.tick(), 250);
  }

  stop() { if (this.timer) window.clearInterval(this.timer); }

  private emit<T extends keyof EventMap>(type: T, data: EventMap[T], ts = this.simMs) {
    this.emitRaw({ v: 2, seq: ++this.seq, ts, type, data } as Envelope);
  }

  private emitSnapshot() {
    const sim = this.simStatus();
    this.emitRaw({
      v: 2, seq: ++this.seq, ts: this.simMs, type: "snapshot",
      data: {
        contract_version: 2, sim, agent_state: this.agentState, recent_metrics: this.history.slice(-480),
        incident: this.incident ?? undefined, hypotheses: [], deploys: this.deploys, scoreboard: this.scoreboard,
        model: this.modelStatus(),
      } satisfies EventMap["snapshot"],
    });
  }

  private simStatus(): EventMap["sim.status"] {
    return {
      scenario: this.chaosHidden ? null : this.scenario, running: true, sim_ms: this.simMs, speed: this.speed,
      fault_active: !!this.fault && this.simMs >= this.fault.start && this.fault.recoverAt === null,
      chaos_hidden: this.chaosHidden,
    };
  }

  private at(t: number, fn: () => void) { this.jobs.push({ at: t, fn }); }

  private tick() {
    this.simMs += 250 * this.speed;
    const due = this.jobs.filter((j) => j.at <= this.simMs).sort((a, b) => a.at - b.at);
    if (due.length) {
      this.jobs = this.jobs.filter((j) => j.at > this.simMs);
      due.forEach((j) => j.fn());
    }
    const m = this.metricsAt(this.simMs);
    this.history.push({ ts: this.simMs, m });
    if (this.history.length > 600) this.history.shift();
    this.emit("metrics.tick", m);
    if (Math.round(this.simMs / 250) % 8 === 0) this.emit("sim.status", this.simStatus());
  }

  // ---- metric model -------------------------------------------------------------------------------
  private damage(t: number): number {
    const f = this.fault;
    if (!f || t < f.start) return 0;
    const up = clamp((t - f.start) / 2500, 0, 1);
    if (f.recoverAt === null || t < f.recoverAt) return up;
    return up * (1 - clamp((t - f.recoverAt) / f.rampMs, 0, 1));
  }

  private bankRates(t: number, damageOverride?: number): Record<Bank, number> {
    const d = damageOverride ?? this.damage(t);
    const f = this.fault;
    const base = (k: number) => HEALTHY + noise(t, k);
    const out: Record<Bank, number> = { BANK_X: base(1), BANK_Y: base(2), BANK_Z: base(3) };
    if (!f) return out;
    const sev = f.severity;
    if (f.kind === "S1") out.BANK_X -= d * sev * 46;
    else if (f.kind === "S2" || f.kind === "S3") out.BANK_X -= d * sev * 61;
    else (Object.keys(out) as Bank[]).forEach((b) => { out[b] -= d * sev * 8; });
    return out;
  }

  private metricsAt(t: number): MetricsTick {
    const r = this.bankRates(t);
    const d = this.damage(t);
    const f = this.fault;
    const biz = f?.kind === "S5";
    const overall = (Object.keys(SHARE) as Bank[]).reduce((a, b) => a + SHARE[b] * r[b], 0);
    const technical = biz ? 99.5 + noise(t, 9) * 0.3 : clamp(overall + 0.8, 0, 100);
    const techPct = biz ? 30 - d * 24 : 30 + d * 67;
    const tps = 820 + Math.sin(t / 5000) * 25 + (biz ? d * 90 : 0);
    const failFrac = (100 - overall) / 100;
    const bank = (b: Bank) => ({
      success_rate: r[b],
      p95_ms: 210 + noise(t, 4) * 20 + (b === "BANK_X" && f && (f.kind === "S2" || f.kind === "S3") ? d * 1400 : 0),
      tps: tps * SHARE[b],
    });
    return {
      success_rate: overall, technical_success_rate: technical,
      decline_mix: { technical_pct: techPct, business_pct: 100 - techPct },
      tps, p95_ms: 215 + d * (f?.kind === "S1" ? 40 : 300),
      by_bank: { BANK_X: bank("BANK_X"), BANK_Y: bank("BANK_Y"), BANK_Z: bank("BANK_Z") },
      paise_failed_per_min: Math.round(tps * 60 * failFrac * 45000 * (biz ? 0.4 : 1)),
    };
  }

  private modelName = "gemma4:e4b";
  private modelSince = 0;
  /** Mock Gemma "warms up" for 5 s after being selected, like the real Ollama model. */
  private modelStatus() {
    return { backend: "ollama" as const, name: this.modelName, healthy: true, warm: Date.now() - this.modelSince > 4000 };
  }

  // ---- REST --------------------------------------------------------------------------------------
  transport(): Transport {
    return {
      request: async (method, path, body) => this.handle(method, path, (body ?? {}) as Record<string, unknown>),
      upload: async (path) => this.handle("POST", path, { voice: true }),
    };
  }

  private followup(incident_id: string, kind: string) {
    const T: Record<string, { title: string; audience: string; body: string }> = {
      permanent_fix: { title: "Make the fix permanent", audience: "internal", body: "## What to change\n- Add a canary step for changes to bank timeouts.\n- Add an alert when a bank's timeout rate passes 5% for 30 seconds.\n\n## Why\nThe rollback restored service, but nothing stops the same change from shipping again." },
      followup_tasks: { title: "Follow-up tasks", audience: "internal", body: "## Checklist\n1. **Owner: payments team.** Review the change that caused the drop (due this week).\n2. **Owner: on-call.** Add the new alert to the runbook.\n3. **Owner: support.** Check that merchants were told." },
      escalate_bank: { title: "Report to BANK_X", audience: "bank", body: "## Summary\nBetween 09:00 and 09:03 about 61% of payments routed to BANK_X failed with server errors.\n\n## What we saw\n- Error rate rose within seconds and stayed high.\n- Other banks were healthy.\n\nPlease confirm the cause and expected recovery time." },
      merchant_notice: { title: "Message for merchants", audience: "merchant", body: "Some payments were slow or failed for a few minutes. The issue is fixed and payments are back to normal. Failed payments were not charged." },
    };
    const t = T[kind] ?? { title: kind, audience: "internal", body: "Draft not available in the mock." };
    window.setTimeout(() => this.emit("followup.ready" as never, { incident_id, kind, simulated: true, ...t } as never), 1800);
    return { ok: true };
  }

  private async handle(method: string, path: string, body: Record<string, unknown>): Promise<unknown> {
    await new Promise((r) => setTimeout(r, 40));
    if (path === "/api/health") return { ok: true, contract_version: 2, models: ["gemma4:e4b", "gemma4:e2b"], active: this.modelName, model: this.modelStatus() };
    if (path === "/api/model" && method === "POST") {
      const m = String(body.model);
      if (m !== "gemma4:e4b" && m !== "gemma4:e2b") return { ok: false, error: "unknown model " + m };
      this.modelName = m; this.modelSince = Date.now();
      return { ok: true, model: this.modelStatus() };
    }
    if (path === "/api/scenario" && method === "POST") {
      this.chaosHidden = false;
      this.startScenario(body.scenario as Scenario, Number(body.severity ?? 1), Number(body.fault_offset_s ?? 6));
      return { ok: true };
    }
    if (path === "/api/reset") { this.reset(); return { ok: true }; }
    if (path === "/api/approve") return this.approve(body);
    if (path === "/api/reject") return this.reject(body);
    if (path === "/api/chaos") return this.chaos();
    if (path === "/api/ask") return this.ask(String(body.text ?? ""), String(body.lang ?? "en"));
    if (path === "/api/voice") return this.ask("BANK_Y pe koi asar hua?", "hi", true);
    if (path === "/api/followup" && method === "POST") return this.followup(String(body.incident_id), String(body.kind));
    if (path === "/api/comms/approve") { this.emit("agent.thought", { incident_id: String(body.incident_id), text: "Comms approved by operator. Nothing is sent; drafts stay in the war-room log." }); return { ok: true }; }
    if (path === "/api/eval/latest") return MOCK_EVAL;
    if (path === "/api/replay/list") return { ok: true, runs: [{ run_id: "run_s1_seed7", scenario: "S1", chaos: false, started: "2026-10-17T09:12:03Z", duration_s: 96, events: 612 }, { run_id: "run_s3_seed3", scenario: "S3", chaos: false, started: "2026-10-17T09:20:41Z", duration_s: 118, events: 744 }, { run_id: "run_chaos_2", scenario: null, chaos: true, started: "2026-10-17T09:31:10Z", duration_s: 74, events: 498 }], active: null };
    if (path === "/api/replay/start") { this.reset(); this.startScenario("S1", 1, 4); return { ok: true }; }
    if (path === "/api/replay/stop") return { ok: true };
    if (path.startsWith("/api/evidence/")) return this.evidence(decodeURIComponent(path.slice("/api/evidence/".length))) ?? { ok: false, error: "unknown evidence" };
    return { ok: false, error: "mock: unhandled " + method + " " + path };
  }

  private reset() {
    this.jobs = []; this.fault = null; this.scenario = null; this.incident = null; this.pending = null;
    this.chaosHidden = false; this.chaosTruth = null; this.agentState = "idle";
    this.deploys = this.deploys.filter((d) => d.number < 482);
    this.simMs += 1; // keep ts monotonic; the reducer detects the reset via sim.status
    this.emitSnapshot();
  }

  // ---- scenarios ---------------------------------------------------------------------------------
  private startScenario(s: Scenario, severity: number, offsetS: number) {
    this.jobs = []; this.pending = null; this.incident = null;
    this.scenario = s;
    if (s === "S0") { this.fault = null; this.emit("sim.status", this.simStatus()); return; }
    const kind: Kind = s === "S1" ? "S1" : s === "S2" ? "S2" : s === "S5" ? "S5" : "S3";
    const start = this.simMs + offsetS * 1000;
    this.fault = { kind, start, severity: clamp(severity, 0.3, 1), recoverAt: null, rampMs: kind === "S5" ? 22000 : 9000 };
    this.emit("sim.status", this.simStatus());
    this.script(kind, start);
  }

  private id(n: string) { return n + "_" + (++this.callN); }

  private script(kind: Kind, f: number) {
    const incId = "INC-" + ++this.incidentN;
    const S = 1000;
    let c = f + 8 * S;
    const step = (gapS: number, fn: () => void) => { c += gapS * S; this.at(c, fn); };
    const thought = (text: string) => this.emit("agent.thought", { incident_id: incId, text: text + " via " + this.modelName });
    const state = (st: EventMap["agent.state"]["state"], note?: string) => { this.agentState = st; this.emit("agent.state", { incident_id: incId, state: st, note }); };
    const hyp = (h: Hypothesis) => this.emit("hypothesis.update", { incident_id: incId, hypothesis: h });
    const tool = (tool: string, args: Record<string, unknown>, summary: string, ev: string[], ms: number, ok = true, gap = 1.2) => {
      const call_id = this.id("c");
      step(gap, () => this.emit("agent.tool_call", { incident_id: incId, call_id, tool, args }));
      step(ms / 1000, () => this.emit("agent.tool_result", { incident_id: incId, call_id, tool, summary, ok, evidence_ids: ev, ms }));
    };

    const biz = kind === "S5";
    const bankOutage = kind === "S2" || kind === "S3";
    const sev = this.fault!.severity;
    const failing = biz ? 0.088 * sev : bankOutage ? 0.61 * sev * 0.4 : 0.46 * sev * 0.4;
    const mk = (extra: Partial<Incident> = {}): Incident => ({
      id: incId, start_ts: f, detected_ts: f + 8 * S, baseline_success: 99.2,
      current_success: 99.2 - failing * 100,
      decline_breakdown: biz
        ? { technical_pct: 6, business_pct: 94, top_codes: [{ code: "BD_LIMIT_EXCEEDED", pct: 81 }, { code: "BD_INSUFFICIENT_FUNDS", pct: 9 }, { code: "BD_WRONG_PIN", pct: 4 }] }
        : { technical_pct: 97, business_pct: 3, top_codes: [{ code: kind === "S1" ? "TD_TIMEOUT" : "TD_BANK_5XX", pct: 71 }, { code: "TD_TIMEOUT", pct: 22 }, { code: "BD_WRONG_PIN", pct: 2 }] },
      blast: biz
        ? { scope: "all banks, UPI collect (campaign cohort)", failing_pct: failing * 100, failed_txns: 4120, paise_per_min_at_risk: 1_150_000 }
        : { scope: "BANK_X via payment-orchestrator", failing_pct: kind === "S1" ? 46 * sev : 61 * sev, failed_txns: kind === "S1" ? 6240 : 7810, paise_per_min_at_risk: kind === "S1" ? 6_100_000 : 7_300_000 },
      ...extra,
    });

    // decoy deploy lands after the dip has started
    if (kind === "S3") {
      this.at(f + 40 * S, () => {
        const d: Deploy = { id: "d_483", service: "notification-service", number: 483, ts: f + 40 * S, author: "m.kapoor", summary: "notification-service: new SMS template", diff: "-  template: otp_v2\n+  template: otp_v3\n   retry: 2" };
        this.deploys = [...this.deploys, d];
        this.emit("deploy.event", d);
      });
    }
    if (kind === "S1") {
      this.at(f - 31 * S > this.simMs ? f - 31 * S : this.simMs + 200, () => {
        const d: Deploy = { id: "d_482", service: "payment-orchestrator", number: 482, ts: f - 31 * S, author: "a.verma", summary: "orchestrator: tighten per-bank timeouts", diff: DIFF_482 };
        this.deploys = [...this.deploys, d];
        this.emit("deploy.event", d);
      });
    }

    // ---- detection + triage
    this.at(c, () => {
      this.incident = mk();
      this.emit("incident.opened", { incident: this.incident });
      state("triage", "Incident opened by detector");
      thought(biz ? "Success rate fell ~8 points across all banks. Checking the decline mix before touching anything." : "BANK_X success rate collapsed while BANK_Y and BANK_Z hold. Pulling the decline breakdown.");
    });
    tool("decline_breakdown", { window: "5m", by: "code" },
      biz ? "94% business declines; BD_LIMIT_EXCEEDED 81%. Technical success rate within baseline." : "97% technical declines; " + (kind === "S1" ? "TD_TIMEOUT 71%" : "TD_BANK_5XX 71%") + ".",
      ["ev_breakdown_1"], 180, true, 1.0);
    step(0.2, () => {
      state("investigating");
      if (biz) {
        hyp({ id: "h1", kind: "business_decline", target: "limit_exceeded", status: "open", reason: "Declines are business-class, not technical" });
        hyp({ id: "h2", kind: "deploy", target: "recent deploys", status: "open", reason: "Check for a correlated change" });
        hyp({ id: "h3", kind: "bank_outage", target: "BANK_X", status: "open", reason: "Check bank health" });
      } else {
        hyp({ id: "h1", kind: "deploy", target: kind === "S1" ? "payment-orchestrator" : "recent deploys", status: "open", reason: "Technical timeouts often follow a change" });
        hyp({ id: "h2", kind: "bank_outage", target: "BANK_X", status: "open", reason: "Failures concentrated on one bank" });
        hyp({ id: "h3", kind: "traffic_spike", target: "gateway", status: "open", reason: "Rate limiting could look like this" });
      }
    });
    tool("success_series", { by: "bank", window: "5m" }, biz ? "All banks down 7 to 9 points together; no single-bank pattern." : "BANK_X " + (kind === "S1" ? "54%" : "38%") + ", BANK_Y 99.1%, BANK_Z 99.3%.", ["ev_metric_bank"], 240);
    if (biz) {
      step(0.6, () => hyp({ id: "h3", kind: "bank_outage", target: "BANK_X", status: "ruled_out", reason: "All banks degrade equally; no bank-specific latency" }));
    } else {
      tool("traffic", { window: "10m" }, "tps 1.02x baseline; gateway 429s at zero.", ["ev_traffic"], 150);
      step(0.4, () => hyp({ id: "h3", kind: "traffic_spike", target: "gateway", status: "ruled_out", reason: "Traffic within 3% of baseline, no rate limiting" }));
    }

    if (kind === "S1") {
      tool("bank_stats", { bank: "BANK_X" }, "BANK_X p95 212 ms, 5xx 0.1%: the bank itself looks normal.", ["ev_metric_bank"], 200);
      step(0.4, () => hyp({ id: "h2", kind: "bank_outage", target: "BANK_X", status: "ruled_out", reason: "BANK_X p95 and 5xx unchanged; timeouts fire client-side" }));
      tool("query_logs", { bank: "BANK_X", level: "error", limit: 20 }, "TIMEOUT after 800ms in payment-orchestrator on 94% of BANK_X failures.", ["ev_log_bank_x"], 310);
      step(0.5, () => hyp({ id: "h1", kind: "deploy", target: "payment-orchestrator", status: "supported", reason: "800 ms timeouts began 31 s after deploy #482" }));
      step(1.0, () => state("concluding"));
      step(1.0, () => thought("Concluding: deploy #482 caused the BANK_X timeouts."));
      step(0.8, () => {
        state("verifying", "Attempt 1");
        this.emit("verify.result", {
          incident_id: incId, attempt: 1, passed: false,
          rejected_reason: "deploy_on_failing_path: cited evidence does not show deploy #482 touching the BANK_X adapter path",
          checks: [
            { name: "evidence_exists", passed: true, detail: "ev_log_bank_x, ev_breakdown_1 resolve" },
            { name: "deploy_precedes_dip", passed: true, detail: "Deploy at T-31s; dip at T" },
            { name: "deploy_on_failing_path", passed: false, detail: "Deploy diff not read; path to BANK_X unconfirmed" },
            { name: "scope_concentration", passed: true, detail: "96% of failures on BANK_X" },
          ],
        });
      });
      step(0.8, () => { state("investigating", "Verifier rejected the first conclusion"); thought("Verifier is right: I never read the diff. Fetching deploy #482."); });
      tool("get_deploy", { id: "482" }, "routing.yaml: BANK_X timeout_ms 3000 -> 800.", ["ev_deploy_482"], 120);
      step(0.5, () => hyp({ id: "h1", kind: "deploy", target: "payment-orchestrator", status: "confirmed", reason: "Diff cuts BANK_X timeout 3000 to 800 ms" }));
    } else {
      tool("bank_stats", { bank: "BANK_X" }, "BANK_X p95 1.6 s, 5xx 18%. BANK_Y and BANK_Z normal.", ["ev_metric_bank"], 200);
      tool("read_bulletin", { bank: "BANK_X" }, "Bulletin: degraded service 09:31 to 09:58. Matches telemetry window.", ["ev_bulletin_x"], 900);
      step(0.5, () => hyp({ id: "h2", kind: "bank_outage", target: "BANK_X", status: "supported", reason: "p95 x6 and 5xx 18% on BANK_X only; bulletin consistent" }));
      if (kind === "S3") {
        step(2.2, () => hyp({ id: "h1", kind: "deploy", target: "notification-service #483", status: "open", reason: "New deploy landed during the incident" }));
        tool("recent_deploys", { window: "30m" }, "Deploy #483 (notification-service) landed 41 s AFTER the dip began.", ["ev_deploy_483"], 140);
        step(0.8, () => state("concluding"));
        step(0.8, () => thought("Deploy #483 is the only recent change. Concluding: #483 caused the failures."));
        step(0.8, () => {
          state("verifying", "Attempt 1");
          this.emit("verify.result", {
            incident_id: incId, attempt: 1, passed: false,
            rejected_reason: "deploy_precedes_dip: deploy #483 landed 41 s after the dip started; it cannot be the cause",
            checks: [
              { name: "evidence_exists", passed: true, detail: "ev_deploy_483 resolves" },
              { name: "deploy_precedes_dip", passed: false, detail: "Deploy at T+41s; dip at T" },
              { name: "deploy_on_failing_path", passed: false, detail: "notification-service is not on the payment path" },
              { name: "no_internal_cause", passed: false, detail: "BANK_X latency x6 points external" },
            ],
          });
        });
        step(0.8, () => { state("investigating", "Verifier rejected the first conclusion"); thought("Rejected on timing and scope. Clearing #483 and re-checking the bank-side evidence."); });
        step(0.6, () => hyp({ id: "h1", kind: "deploy", target: "notification-service #483", status: "ruled_out", reason: "Landed 41 s after the dip; off the payment path" }));
        step(0.6, () => hyp({ id: "h2", kind: "bank_outage", target: "BANK_X", status: "confirmed", reason: "Latency, 5xx and bulletin all point at BANK_X" }));
      } else {
        step(0.6, () => hyp({ id: "h1", kind: "deploy", target: "recent deploys", status: "ruled_out", reason: "No deploys in the 30 minutes before the dip" }));
        step(0.6, () => hyp({ id: "h2", kind: "bank_outage", target: "BANK_X", status: "confirmed", reason: "Latency, 5xx and bulletin all point at BANK_X" }));
        step(0.8, () => state("concluding"));
      }
    }
    if (biz) {
      tool("recent_deploys", { window: "30m" }, "No deploys on the payment path. Campaign flag 'cashback_oct' went live 6 min ago.", ["ev_deploy_none"], 130);
      step(0.4, () => hyp({ id: "h2", kind: "deploy", target: "recent deploys", status: "ruled_out", reason: "Nothing deployed on the payment path" }));
      step(0.6, () => hyp({ id: "h1", kind: "business_decline", target: "limit_exceeded", status: "supported", reason: "BD_LIMIT_EXCEEDED 81% of declines; infra healthy" }));
      step(0.8, () => { state("concluding"); thought("A cashback campaign pushed users past daily limits. Concluding business decline."); });
      step(0.8, () => {
        state("verifying", "Attempt 1");
        this.emit("verify.result", {
          incident_id: incId, attempt: 1, passed: false,
          rejected_reason: "decline_class_matches: cause class is infra but 94% of declines are business-class",
          checks: [
            { name: "evidence_exists", passed: true, detail: "ev_breakdown_1 resolves" },
            { name: "decline_class_matches", passed: false, detail: "Infra claim needs >=70% technical declines; found 6%" },
            { name: "others_healthy", passed: true, detail: "Technical success within baseline" },
          ],
        });
      });
      step(0.8, () => { state("investigating", "Verifier rejected the first conclusion"); thought("Claim classed as infra by mistake. Restating as business decline."); });
      step(0.5, () => hyp({ id: "h1", kind: "business_decline", target: "limit_exceeded", status: "confirmed", reason: "94% business declines; technical success at baseline" }));
    }

    // ---- verify (pass), RCA, preview, proposal
    step(0.9, () => {
      state("verifying", "Attempt 2");
      const names = biz ? ["evidence_exists", "decline_class_matches", "others_healthy", "action_matches_cause"]
        : kind === "S1" ? ["evidence_exists", "deploy_precedes_dip", "deploy_on_failing_path", "scope_concentration", "others_healthy", "action_matches_cause"]
          : ["evidence_exists", "no_internal_cause", "scope_concentration", "others_healthy", "external_claim_consistent", "action_matches_cause"];
      this.emit("verify.result", { incident_id: incId, attempt: 2, passed: true, checks: names.map((n) => ({ name: n, passed: true, detail: "ok" })) });
    });

    const actionId = "act_" + incId.slice(4);
    const action: EventMap["action.proposed"]["action"] = biz
      ? { id: actionId, kind: "notify_only", params: { channel: "merchant+support" }, label: "Notify merchants and support", rationale: "Nothing is broken. Users hit daily limits during the cashback campaign; warn them instead of rolling anything back." }
      : kind === "S1"
        ? { id: actionId, kind: "rollback_deploy", params: { deploy_id: "482", service: "payment-orchestrator" }, label: "Roll back #482", rationale: "Restores BANK_X timeout to 3000 ms. Rollback is idempotent." }
        : { id: actionId, kind: "failover_bank", params: { from_bank: "BANK_X", to_bank: "BANK_Y", bank: "BANK_X" }, label: "Fail over BANK_X to BANK_Y", rationale: "Route BANK_X traffic via BANK_Y until the bank recovers. Reversible.",
          warnings: kind === "S3" ? ["BANK_Y would run at 103% of capacity", "This works around the problem; BANK_X is still down"] : ["BANK_Y would run at 92% of capacity", "This works around the problem; BANK_X is still down"] } as EventMap["action.proposed"]["action"];
    const cause = biz ? "business:limit_exceeded" : kind === "S1" ? "deploy:482" : "bank:BANK_X";
    const evidence: EventMap["rca.ready"]["rca"]["evidence"] = biz
      ? [{ id: "ev_breakdown_1", kind: "breakdown", title: "Decline mix: 94% business" }, { id: "ev_deploy_none", kind: "deploy", title: "No deploys on payment path" }]
      : kind === "S1"
        ? [{ id: "ev_deploy_482", kind: "deploy", title: "Deploy #482 diff" }, { id: "ev_log_bank_x", kind: "log", title: "800 ms timeouts, BANK_X" }, { id: "ev_breakdown_1", kind: "breakdown", title: "Decline mix: 97% technical" }]
        : [{ id: "ev_bulletin_x", kind: "bulletin", title: "BANK_X bulletin (consistent)" }, { id: "ev_metric_bank", kind: "metric", title: "BANK_X p95 x6, 5xx 18%" }, ...(kind === "S3" ? [{ id: "ev_deploy_483", kind: "deploy" as const, title: "Deploy #483 (cleared)" }] : [])];
    const ruled: EventMap["rca.ready"]["rca"]["ruled_out"] = biz
      ? [{ target: "bank_outage:BANK_X", reason: "All banks degrade equally" }, { target: "deploy", reason: "Nothing deployed on payment path" }]
      : kind === "S1"
        ? [{ target: "bank_outage:BANK_X", reason: "Bank p95 and 5xx unchanged" }, { target: "traffic_spike", reason: "Traffic within 3% of baseline" }]
        : [{ target: kind === "S3" ? "deploy:483" : "deploy", reason: kind === "S3" ? "Landed 41 s after the dip; off the payment path" : "No deploys before the dip" }, { target: "traffic_spike", reason: "Traffic within 3% of baseline" }];
    step(0.8, () => {
      state("previewing");
      this.emit("rca.ready", {
        rca: {
          incident_id: incId, cause, confidence: biz ? 0.91 : kind === "S1" ? 0.94 : 0.9,
          cause_label: biz ? "Cashback campaign pushed users past daily limits" : kind === "S1" ? "Deploy #482 cut BANK_X timeout from 3000 to 800 ms" : "BANK_X outage (external)",
          evidence, ruled_out: ruled,
          summary: biz ? "Business declines (BD_LIMIT_EXCEEDED) account for 81% of failures. Infrastructure is healthy. Recommend merchant and support comms, not a rollback."
            : kind === "S1" ? "Deploy #482 shortened the BANK_X timeout. BANK_X answers in about 210 ms at p95 but the orchestrator now aborts at 800 ms under load."
              : "BANK_X latency rose x6 and 5xx reached 18%. The bank's own bulletin matches the telemetry window. Internal services are healthy.",
          proposed_action: action,
        },
      });
    });
    step(1.0, () => {
      const t0 = this.simMs;
      const predicted: [number, number][] = [];
      const baseline: [number, number][] = [];
      const fakeRecover = t0 + 2000;
      const saved = this.fault!;
      for (let i = 0; i <= 24; i++) {
        const t = t0 + i * 1000;
        const keep = saved.recoverAt;
        saved.recoverAt = biz ? null : fakeRecover;
        const dp = this.damage(t);
        saved.recoverAt = keep;
        const rp = this.bankRates(t, biz ? this.damage(t) : dp);
        const rb = this.bankRates(t);
        const ov = (r: Record<Bank, number>) => (Object.keys(SHARE) as Bank[]).reduce((a, b) => a + SHARE[b] * r[b], 0);
        predicted.push([t, ov(rp)]); baseline.push([t, ov(rb)]);
      }
      this.emit("twin.preview", { incident_id: incId, action_id: actionId, kind: action.kind, recovers: !biz && kind !== "S3", expected_recovery_ms: biz ? 0 : 8000, predicted: biz ? baseline : predicted, baseline, ...(action.kind === "failover_bank" ? { target_bank: "BANK_Y", recovery_pct: kind === "S3" ? 78 : 97, target_headroom_pct: kind === "S3" ? -3 : 8 } : {}) } as EventMap["twin.preview"]);
    });
    step(0.6, () => {
      state("proposing");
      const token = "tok_" + Math.random().toString(36).slice(2, 10);
      const preview = { recovers: !biz, expected_recovery_ms: biz ? 0 : 8000, series: [] as [number, number][] };
      this.pending = { incident_id: incId, action_id: actionId, token, kind: action.kind, params: action.params, chosen: kind };
      this.emit("action.proposed", { incident_id: incId, action: { ...action, preview }, approval_token: token, expires_ts: this.simMs + 300_000 });
      state("awaiting_approval", "Waiting for operator");
    });
  }

  private approve(body: Record<string, unknown>) {
    const p = this.pending;
    if (!p || body.token !== p.token || body.action_id !== p.action_id) return { ok: false, error: "invalid or expired approval token" };
    this.pending = null;
    const f = this.fault!;
    const incId = p.incident_id;
    const t0 = this.simMs;
    this.agentState = "remediating";
    this.emit("agent.state", { incident_id: incId, state: "remediating", note: "Operator approved" });
    const biz = p.chosen === "S5";
    f.recoverAt = t0 + 1500;
    const recoveryMs = biz ? 0 : 9000;
    this.at(t0 + 1500 + (biz ? 800 : recoveryMs), () => {
      this.emit("action.executed", { incident_id: incId, action_id: p.action_id, kind: p.kind, predicted_recovery_ms: biz ? undefined : 8000, actual_recovery_ms: biz ? undefined : recoveryMs, recovered: true });
      this.emit("impact.savings", {
        incident_id: incId,
        actual_loss_paise: biz ? 900_000 : 2_300_000, counterfactual_loss_paise: biz ? 14_400_000 : 36_300_000,
        saved_paise: biz ? 13_500_000 : 34_000_000, assumed_manual_mttr_ms: 25 * 60_000,
      });
      this.agentState = "comms"; this.emit("agent.state", { incident_id: incId, state: "comms" });
    });
    const base = t0 + 1500 + (biz ? 800 : recoveryMs);
    this.at(base + 1800, () => {
      this.emit("comms.drafted", { incident_id: incId, drafts: commsFor(p.chosen) });
      this.agentState = "postmortem"; this.emit("agent.state", { incident_id: incId, state: "postmortem" });
    });
    this.at(base + 3600, () => {
      this.emit("postmortem.ready", { incident_id: incId, markdown: postmortemFor(p.chosen, incId) });
      const end = this.simMs;
      this.emit("incident.closed", { incident_id: incId, end_ts: end, mttr_ms: end - (this.incident?.start_ts ?? end) });
      this.agentState = "idle"; this.emit("agent.state", { incident_id: incId, state: "idle" });
      if (this.chaosHidden && this.chaosTruth) {
        this.chaosHidden = false;
        const ok = true;
        const x = this.chaosTruth;
        this.scoreboard = [...this.scoreboard, { round: this.chaosRound, correct: ok, scenario: x.scenario, agent_cause: x.truth }];
        this.emit("sim.status", this.simStatus());
        this.emit("chaos.revealed", { round: this.chaosRound, scenario: x.scenario, ground_truth: x.truth, agent_cause: x.truth, correct: ok });
        this.chaosTruth = null;
      }
    });
    return { ok: true };
  }

  private reject(body: Record<string, unknown>) {
    const p = this.pending;
    if (!p) return { ok: false, error: "nothing pending" };
    this.pending = null;
    this.agentState = "inconclusive";
    this.emit("agent.state", { incident_id: String(body.incident_id), state: "inconclusive", note: "Operator rejected the proposed action" });
    return { ok: true };
  }

  private chaos() {
    this.chaosRound += 1;
    const pool: { s: Scenario; truth: string }[] = [
      { s: "S1", truth: "deploy:482" }, { s: "S2", truth: "bank:BANK_X" }, { s: "S3", truth: "bank:BANK_X" }, { s: "S5", truth: "business:limit_exceeded" },
    ];
    const pick = pool[Math.floor(Math.random() * pool.length)];
    this.chaosHidden = true;
    this.chaosTruth = { scenario: pick.s, truth: pick.truth };
    this.emit("chaos.armed", { round: this.chaosRound });
    this.startScenario(pick.s, 0.8 + Math.random() * 0.2, 4 + Math.floor(Math.random() * 6));
    this.chaosHidden = true;
    this.emit("sim.status", this.simStatus());
    return { ok: true, round: this.chaosRound };
  }

  private ask(text: string, lang: string, fromVoice = false) {
    const qid = "q_" + ++this.queryN;
    const t = text.toLowerCase();
    this.emit("voice.query", { query_id: qid, transcript: text, lang: fromVoice ? "hi" : lang });
    const mentionsY = t.includes("bank_y") || t.includes("bank y");
    const faultOn = !!this.fault && this.simMs > this.fault.start && this.fault.recoverAt === null;
    const hi = fromVoice || lang === "hi" || /\b(pe|hua|kya|asar)\b/.test(t);
    let ans: string;
    if (mentionsY) ans = hi ? "BANK_Y pe koi asar nahi hua. Success rate 99.1% hai. Dikkat sirf BANK_X pe hai." : "BANK_Y is not affected: success rate is 99.1%. The failures are isolated to BANK_X.";
    else if (faultOn) ans = hi ? "Abhi ek incident chal raha hai. Verified cause investigation ke baad RCA card mein dikhega." : "An incident is in progress. The verified cause will appear on the RCA card once the verifier passes.";
    else ans = hi ? "Abhi sab systems normal hain. ProofLine nazar rakh raha hai." : "All systems nominal. ProofLine is watching.";
    window.setTimeout(() => this.emit("voice.answer", { query_id: qid, text: ans, lang: hi ? "hi" : "en", evidence_ids: mentionsY ? ["ev_metric_bank"] : [] }), 900 / this.speed);
    return { ok: true, query_id: qid };
  }

  private evidence(id: string): Evidence | null {
    const E: Record<string, Evidence> = {
      ev_breakdown_1: { id, kind: "breakdown", title: "Decline breakdown, last 5 min", body: { window: "5m", technical_pct: 97, business_pct: 3, top_codes: "TD_TIMEOUT 71%, TD_BANK_5XX 22%, BD_WRONG_PIN 2%" } },
      ev_metric_bank: { id, kind: "metric", title: "Success rate by bank", body: { BANK_X: "38.4%", BANK_Y: "99.1%", BANK_Z: "99.3%", note: "Failures concentrated on BANK_X" } },
      ev_traffic: { id, kind: "metric", title: "Traffic vs baseline", body: { tps_ratio: 1.02, gateway_429: 0 } },
      ev_deploy_482: { id, kind: "deploy", title: "Deploy #482 payment-orchestrator", ts: this.fault ? this.fault.start - 31_000 : EPOCH, body: { service: "payment-orchestrator", number: 482, author: "a.verma", summary: "orchestrator: tighten per-bank timeouts", landed_before_dip_s: 31, diff: DIFF_482 } },
      ev_deploy_483: { id, kind: "deploy", title: "Deploy #483 notification-service", body: { service: "notification-service", number: 483, author: "m.kapoor", summary: "new SMS template", landed_after_dip_s: 41, diff: "-  template: otp_v2\n+  template: otp_v3" } },
      ev_deploy_none: { id, kind: "deploy", title: "Deploys in last 30 minutes", body: { payment_path_deploys: 0, unrelated: "wallet #481 (60 min ago)" } },
      ev_log_bank_x: { id, kind: "log", title: "Error logs, BANK_X", body: { total: 5120, lines: [
        { ts: EPOCH + 1000, service: "payment-orchestrator", level: "ERROR", msg: "bank-adapter[BANK_X] TIMEOUT after 800ms txn=t_88213" },
        { ts: EPOCH + 1250, service: "payment-orchestrator", level: "ERROR", msg: "bank-adapter[BANK_X] TIMEOUT after 800ms txn=t_88217" },
        { ts: EPOCH + 1900, service: "upi-service", level: "WARN", msg: "retry exhausted for BANK_X (1/1)" },
      ] } },
      ev_bulletin_x: { id, kind: "bulletin", title: "BANK_X status bulletin", body: { bulletin: {
        image_url: bulletinSvg("BANK_X", "Degraded service", "09:31", "09:58"),
        extracted: { bank: "BANK_X", status: "degraded", claimed_start_ts: EPOCH + 60_000, claimed_end_ts: EPOCH + 1_680_000 },
        consistent: true, detail: "Bulletin window starts 4 s from the telemetry change point and matches BANK_X-only failures.",
      } } },
    };
    return E[id] ?? null;
  }
}

function commsFor(kind: Kind): EventMap["comms.drafted"]["drafts"] {
  if (kind === "S5") return [
    { audience: "internal", lang: "en", text: "[#payments-oncall] Success rate dipped ~8 pts because users hit daily limits during the cashback campaign. Infra is healthy; no rollback needed. Support and merchant comms drafted." },
    { audience: "internal", lang: "hi", text: "[#payments-oncall] Cashback campaign ke dauran users daily limit tak pahunch gaye, isliye success rate ~8 point gira. Infra theek hai; rollback ki zaroorat nahi." },
    { audience: "merchant", lang: "en", text: "Some customers may see 'daily limit exceeded' during today's cashback offer. Payments are working normally; limits reset at midnight." },
    { audience: "merchant", lang: "hi", text: "Aaj ke cashback offer mein kuch customers ko 'daily limit exceeded' dikh sakta hai. Payments normal hain; limit raat 12 baje reset hoti hai." },
    { audience: "customer", lang: "en", text: "You have reached today's UPI limit. Try again after midnight or use another payment method." },
    { audience: "customer", lang: "hi", text: "Aap aaj ki UPI limit tak pahunch gaye hain. Raat 12 baje ke baad try karein ya doosra payment method chunein." },
  ];
  const what = kind === "S1" ? "a deploy that shortened the BANK_X timeout (rolled back)" : "a BANK_X outage (traffic failed over to BANK_Y)";
  const whatHi = kind === "S1" ? "ek deploy jisne BANK_X timeout ghata diya (rollback ho gaya)" : "BANK_X outage (traffic BANK_Y pe failover hua)";
  return [
    { audience: "internal", lang: "en", text: "[#payments-oncall] Resolved: " + what + ". Success rate back to 99%. Postmortem drafted." },
    { audience: "internal", lang: "hi", text: "[#payments-oncall] Resolve: " + whatHi + ". Success rate wapas 99%. Postmortem draft ready hai." },
    { audience: "merchant", lang: "en", text: "Between 09:31 and 09:40 some UPI payments failed or were delayed. Service is restored. Failed payments are not debited." },
    { audience: "merchant", lang: "hi", text: "09:31 se 09:40 ke beech kuch UPI payments fail ya late hue. Service wapas normal hai. Fail hue payments debit nahi hote." },
    { audience: "customer", lang: "en", text: "Some payments failed briefly. Everything works again. Any debited amount is refunded automatically." },
    { audience: "customer", lang: "hi", text: "Kuch payments thodi der ke liye fail hue. Ab sab theek hai. Debit hui raashi apne aap refund ho jaayegi." },
  ];
}

function postmortemFor(kind: Kind, id: string): string {
  const cause = kind === "S5" ? "Business-decline surge (daily limits during cashback campaign)" : kind === "S1" ? "Deploy #482 shortened BANK_X timeout (3000 to 800 ms)" : "BANK_X outage (external)";
  const action = kind === "S5" ? "Notify merchants and support (no infrastructure change)" : kind === "S1" ? "Roll back deploy #482" : "Fail over BANK_X traffic to BANK_Y";
  return [
    "# Postmortem " + id,
    "",
    "**Cause:** " + cause,
    "",
    "## Timeline (simulated clock)",
    "- 09:31:00 fault begins",
    "- 09:31:08 detector opens incident",
    "- 09:31:40 verifier rejects first conclusion; agent corrects",
    "- 09:32:10 verified RCA, fix previewed in twin",
    "- 09:32:25 operator approves: " + action,
    "",
    "## Impact",
    "- Peak failing share of transactions: " + (kind === "S5" ? "8.8%" : kind === "S1" ? "18.4%" : "24.4%"),
    "- Estimated loss avoided vs a 25 min manual response: **₹3.40 L** (assumption: fault persists 25 min at the measured failure rate)",
    "",
    "## What went well",
    "- The verifier caught a weak first claim before it reached the operator.",
    "- The twin predicted recovery in 8 s; actual recovery took 9 s.",
    "",
    "## Action items",
    "1. Add a config lint: per-bank timeouts below 1500 ms need review.",
    "2. Subscribe to the bank bulletin feed for automatic cross-checks.",
    "3. Pre-announce limit changes ahead of campaigns.",
  ].join("\n");
}

const MOCK_EVAL: EvalReport & { ok: true } = {
  ok: true, generated: "2026-10-17T08:12:00Z", model: "mock-gemma-4-e4b (illustrative numbers)", total_runs: 132, false_incident_rate: 0,
  rows: [
    { scenario: "S0", runs: 12, accuracy: 1, median_rca_s: 0, p90_rca_s: 0, mean_tool_calls: 0, verifier_rejection_rate: 0, twin_error_s: 0 },
    { scenario: "S1", runs: 30, accuracy: 0.97, median_rca_s: 41, p90_rca_s: 58, mean_tool_calls: 6.2, verifier_rejection_rate: 0.27, twin_error_s: 1.1 },
    { scenario: "S2", runs: 30, accuracy: 0.93, median_rca_s: 44, p90_rca_s: 63, mean_tool_calls: 6.8, verifier_rejection_rate: 0.17, twin_error_s: 1.4 },
    { scenario: "S3", runs: 30, accuracy: 0.9, decoy_cleared_pct: 93, median_rca_s: 52, p90_rca_s: 74, mean_tool_calls: 7.9, verifier_rejection_rate: 0.43, twin_error_s: 1.3 },
    { scenario: "S5", runs: 30, accuracy: 0.97, correctly_not_remediated_pct: 100, median_rca_s: 36, p90_rca_s: 51, mean_tool_calls: 5.4, verifier_rejection_rate: 0.2, twin_error_s: 0 },
  ],
  ablations: [
    { name: "Full pipeline (E4B + verifier)", accuracy: 0.94, runs: 120 },
    { name: "Verifier off", accuracy: 0.71, runs: 120 },
    { name: "E2B + verifier", accuracy: 0.83, runs: 120 },
    { name: "E2B, verifier off", accuracy: 0.58, runs: 120 },
  ],
};
