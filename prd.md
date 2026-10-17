# PRD — Proofline: the AI on-call engineer for payments that proves its answers

> **Event:** Hacktober Hack Day (Paytm office) · **Track:** Best Use of Google Gemma · **Model:** Gemma 4 (mandatory)
> **Version:** 2.0 (supersedes v1) · **Companion docs:** `design.md` (architecture, contract, UI), `CLAUDE.md` (backend build rules)
> **Build split:** Codex → `frontend/` · Claude Code → `backend/`, `eval/`, `contracts/`

---

## 0. TL;DR for the team

v1 already had a strong core: an agent that investigates a payment success-rate dip, a **deterministic verifier** that must accept its conclusion, and human-approved remediation. v2 keeps all of that and adds six things that make the project hard to beat in a room full of Paytm engineers:

| # | Differentiator | Why it wins |
|---|---|---|
| 1 | **Fix Preview (digital twin)** — every proposed fix is first run in a forked copy of the simulator; the UI shows a ghost "predicted recovery" line before anyone clicks Approve | No commercial AI SRE shows you the future before you act. Turns "trust the AI" into "watch the AI's fix work, then approve" |
| 2 | **Technical vs business decline triage** — the agent separates infra failures (timeouts, bank 5xx) from user-side failures (wrong PIN, limit exceeded) and refuses to "fix" what isn't broken | This is exactly how UPI success rate is measured in India. Paytm judges will recognise it instantly |
| 3 | **Chaos Mode (blind test)** — a judge presses the button; the fault is picked at random and hidden from the presenter; ground truth is revealed only after the agent answers | Proves the demo isn't scripted. Credibility is the #1 thing hackathon AI demos lack |
| 4 | **Voice war-room (Gemma 4 native audio)** — push-to-talk follow-up questions in English, Hindi or Hinglish ("BANK_X ka kya scene hai?"), answered with live tool calls | Uses a Gemma 4 E4B capability almost nobody else will touch: audio input on a laptop GPU |
| 5 | **Multimodal evidence** — the agent reads a bank's status-bulletin *image* and the verifier cross-checks its claims against telemetry ("bulletin says maintenance ended 14:20; failures continued until 14:31 — claim contradicted") | Gemma vision + "trust but verify" applied to outside sources, not just the model |
| 6 | **₹ Saved counter + honest eval board** — live "loss avoided vs a 25-minute human MTTR" and an on-screen accuracy table including a *verifier on vs off* ablation | Judges remember a number. Ablation proves the verifier matters |

Everything is split into **4 phases** (§13). Phase 2 alone is a complete, winnable demo; Phases 3–4 are what take it from good to first place.

---

## 1. One-line pitch

**When payment success rates drop, Proofline investigates like a senior SRE, shows its reasoning live, proves its root cause with evidence a rules engine checks, previews the fix in a simulation, and only acts after a human says yes — all on a laptop, offline, with Gemma 4.**

## 2. Problem

- When success rates dip, the on-call engineer spends the first 15–30 minutes jumping between dashboards, logs, deploy history and bank status pages to answer one question: **what changed, and where?**
- In UPI the stakes are national-scale. Failures split into **technical declines** (bank/NPCI system issues, timeouts, overload) and **business declines** (wrong PIN, limits, beneficiary errors). Industry-wide technical declines fell from ~8–10% in 2016 to ~0.7–0.8% by 2025, and the government is now pushing banks and NPCI to cut failures further, with NPCI reporting downtime for 11 banks in a single month in 2026. Lack of visibility into *why* a transaction failed is called out as a blocker to higher success rates.
- Existing tools tell you **that** something broke. AI SRE products (Datadog Bits AI, incident.io, Resolve AI and others) now generate hypotheses and RCAs, but their accuracy claims are mostly self-reported, they are cloud SaaS tied to their own telemetry platforms, and none show you what a fix will do before you apply it.

## 3. Core insight

> **An AI root-cause claim is worthless unless it can be checked — and an AI fix is dangerous unless it can be previewed.**

Proofline therefore enforces a three-gate pipeline:

1. **Evidence gate** — every claim cites evidence IDs that resolve to real records.
2. **Logic gate** — a deterministic verifier checks timing, scope and cause/action consistency.
3. **Outcome gate** — the proposed action is executed first in a forked simulator; it can only be offered for approval if the twin predicts recovery.

The model **proposes**; code **decides**; a human **approves**.

## 4. Users

| User | Need | What Proofline gives them |
|---|---|---|
| On-call SRE / payments engineer (primary) | Fast, trustworthy diagnosis; safe action | Verified RCA in < 1 min, fix preview, one-click approve, voice follow-ups |
| Engineering manager | Impact and communication | Blast radius in ₹, ₹ saved, auto-drafted stakeholder comms, postmortem |
| Business / ops team | "Is this us or the users?" | Technical-vs-business decline split with plain-language explanation |
| Hackathon judges | Understand in 30 s; believe it's real | Chaos Mode, live eval board, verifier rejections shown proudly |

## 5. Goals and non-goals

### Goals
- **G1** Detect a dip and start investigating with zero clicks.
- **G2** Find the correct root cause for every seeded fault, including a decoy and a business-decline surge.
- **G3** Every claim in the final report links to verifier-confirmed evidence.
- **G4** Every proposed fix is previewed in a digital twin before approval; approved fixes visibly restore health.
- **G5** Run fully offline on the target laptop (i5-12th gen, 16 GB RAM, RTX 3050 6 GB) with local Gemma 4 E4B.
- **G6** Use Gemma 4 for what it is uniquely good at: native function calling, audio input, image input, multilingual output.
- **G7** Publish honest, reproducible accuracy numbers, including an ablation that shows the verifier's value.

### Non-goals
- Real payment systems, real data or real bank names (everything synthetic; banks are `BANK_X/Y/Z`).
- Autonomous remediation without human approval.
- Auth, multi-tenancy, persistence beyond a session.
- A generic "chat with your logs" bot.

## 6. Domain model (realism that Paytm judges will notice)

- **Path:** `gateway → payment-orchestrator → upi-service → bank-adapter[BANK_X|Y|Z]`, plus `wallet` and `notification-service` as noise.
- **Banks are partner/PSP banks** on the acquiring side, which makes **routing failover between banks** a realistic remediation.
- **Synthetic decline codes** (deliberately not real NPCI codes):
  - Technical: `TD_TIMEOUT`, `TD_BANK_5XX`, `TD_RATE_LIMITED`
  - Business: `BD_WRONG_PIN`, `BD_LIMIT_EXCEEDED`, `BD_INSUFFICIENT_FUNDS`
- **Success rate** is reported overall, per bank, and split into **technical success rate** (excluding business declines) vs raw success rate — mirroring how the industry tracks it.
- **Money** in integer paise; average ticket ~₹450.

## 7. Scenarios

All scenarios are deterministic given `(seed, scenario, fault_offset, severity)` and have a known ground truth.

| ID | Scenario | Ground truth | What makes it hard | Phase |
|---|---|---|---|---|
| **S1** | **Bad deploy** — deploy `#482` to `payment-orchestrator` cuts BANK_X timeout 3000 → 800 ms | `deploy:482` | Link a config change to bank-specific timeouts | 2 |
| **S2** | **Bank outage** — BANK_X latency ×6, 5xx 0.1% → 18%; BANK_X publishes a status bulletin (image) | `bank:BANK_X` | Don't blame internal services; read and cross-check the bulletin | 3 |
| **S3** | **Decoy** — BANK_X outage; unrelated deploy `#483` (`notification-service`) lands ~40 s *after* the dip starts | `bank:BANK_X` | Clear the deploy using timing and scope | 3 |
| **S5** | **Business-decline surge** — a cashback campaign drives users past daily limits; `BD_LIMIT_EXCEEDED` spikes; infra is healthy | `business:limit_exceeded` | Recognise that nothing is broken; recommend comms, not rollback | 3 |
| S4 | Traffic spike — 3× load, gateway rate-limits | `traffic_spike` | Distinguish load from failure | 4 (stretch) |
| S6 | Compound — bad deploy *and* a mild bank degradation | `deploy:482` + `bank:BANK_Y` | Report two causes with separate evidence | 4 (stretch) |
| S0 | No fault (eval only) | none | No false incidents | 1 |

## 8. Demo flow (target: 4 minutes)

1. **Healthy** (15 s): success ~99%, ₹ at risk 0, "All systems nominal. Proofline is watching."
2. **Break: bad deploy** (S1). Chart dips within ~10 s; incident opens on its own.
3. **Investigation live** (30 s): hypothesis cards appear; tool calls stream; cards get ruled out with one-line reasons; decline split shows 97% technical.
4. **Verifier moment**: if the model's first claim is weak, the amber "Verifier rejected: …" row appears and the agent self-corrects. (Shown proudly.)
5. **RCA card**: cause, confidence, blast radius, verified evidence chips. Click a chip → deploy diff with *deploy before dip* on the timeline.
6. **Fix Preview**: a ghost line projects recovery for "Roll back #482" — "predicted back to 99% in ~8 s". Click **Approve** → the real line follows the ghost.
7. **₹ Saved**: "₹3.4L loss avoided vs a 25-min manual response."
8. **Voice** (20 s): presenter holds the mic button: *"BANK_Y pe koi asar hua?"* → agent runs a breakdown tool and answers in Hinglish.
9. **Chaos Mode** (60 s): hand the button to a judge. Hidden fault (e.g. S3 decoy). Agent clears deploy #483 because it landed 41 s after the dip. Reveal: ✓ correct.
10. **Close on the eval board**: accuracy across seeded runs, decoy cleared %, verifier catches, verifier-off ablation.

## 9. Functional requirements

### 9.1 Simulator & digital twin
- **FR-1** Generate vectorized synthetic traffic across the topology in §6 with per-transaction outcome, decline code, latency and amount.
- **FR-2** Emit structured logs (failures sampled 1:4, deploy/config events, Poisson noise), metric rollups and a deploy/config history.
- **FR-3** Fault injection for every scenario in §7 and a **Reset** that restores the baseline exactly.
- **FR-4** Deterministic given `(seed, scenario, fault_offset, severity)`; simulated clock decoupled from wall time (real-time for demo, fast-forward for eval/twin).
- **FR-5** Whitelisted remediations: `rollback_deploy`, `failover_bank`, `enable_circuit_breaker`, `scale_gateway`, `notify_only` (for business declines).
- **FR-6 (new) Fork.** The engine can snapshot its full state (config, RNG state, clock, buffers) and run a copy in fast-forward without touching the live sim.
- **FR-7 (new) Fix Preview.** For each candidate action, run the fork for 120 s sim time with the action applied and with no action; return the predicted success-rate series, time-to-recovery and a `recovers` boolean. Budget: < 300 ms wall per preview.
- **FR-8 (new) Bank bulletins.** In S2/S3 the simulator renders a PNG status bulletin for BANK_X (headline, affected services, claimed window). The bulletin's claimed window may be deliberately wrong in some seeds.

### 9.2 Detection
- **FR-9** Deterministic detector: open an incident when 30 s success rate falls ≥ 5 pp below the 5 min baseline for ≥ 10 s. One open incident at a time; debounce; ignore during reset.
- **FR-10** Back-date `incident.start_ts` with a change-point search.
- **FR-11 (new)** Attach a **decline breakdown** to every incident: % technical vs % business, top decline codes, and top banks.

### 9.3 Agent
- **FR-12** Bounded state machine: `TRIAGE → INVESTIGATE → CONCLUDE → VERIFY → PREVIEW → PROPOSE → AWAIT_APPROVAL → REMEDIATE → COMMS → POSTMORTEM`.
- **FR-13** Gemma 4 native function calling over a fixed tool set (design.md §5.5) plus new tools: `decline_breakdown(window, by)`, `read_bulletin(bank)` (image input), `preview_fix(action)`.
- **FR-14** Hypothesis board with seeded kinds `deploy | bank_outage | traffic_spike | config_drift | business_decline`; statuses `open | supported | ruled_out | confirmed`; reasons ≤ 120 chars, all set via the `update_hypothesis` tool.
- **FR-15** Budgets: 10 tool calls, 3 verify attempts, 30 s per model call, 90 s total. Over budget → honest `inconclusive` with best partial evidence.
- **FR-16** Structured conclusion: `cause`, `confidence`, `evidence_ids[]`, `ruled_out[]`, `proposed_action`, `summary`.
- **FR-17** Every thought summary, tool call/result, hypothesis change and verdict is streamed and recorded.
- **FR-18 (new) Skeptic pass (Phase 4 stretch).** Before the verifier, a second short Gemma call is prompted to argue against the conclusion; any objection it raises that maps to a verifier rule is checked explicitly.

### 9.4 Verifier
- **FR-19** Deterministic checks before any conclusion is shown: `evidence_exists`, `deploy_precedes_dip`, `deploy_on_failing_path`, `scope_concentration`, `others_healthy`, `no_internal_cause`, `traffic_elevated`, `action_matches_cause`.
- **FR-20 (new)** `decline_class_matches` — a business cause requires ≥ 70% business declines with technical success rate within baseline; an infra cause requires the reverse.
- **FR-21 (new)** `external_claim_consistent` — any bulletin cited as evidence is compared with telemetry; contradictions are reported to the agent and shown in the UI.
- **FR-22 (new)** `twin_predicts_recovery` — the proposed action must recover in the fork; otherwise the agent must pick another action or report that no safe action exists.
- **FR-23** Failed checks return machine-readable reasons to the agent; **verifier catches are counted** and displayed.

### 9.5 Remediation, approval & comms
- **FR-24** Agent only proposes. Execution needs a UI approval carrying a single-use token bound to `(incident_id, action_id)`, expiring in 5 min.
- **FR-25** Remediation is idempotent; recovery visible within ~10 s and compared to the twin's prediction ("predicted 8 s, actual 9 s").
- **FR-26 (new) Stakeholder comms.** After an RCA, the agent drafts: an internal Slack-style update, a merchant notice, and a customer in-app banner in **English and Hindi**. Drafts are shown for human approval; nothing is "sent" (no real channels).

### 9.6 Impact & reporting
- **FR-27** Blast radius computed in code: scope, % failing, failed txn count, ₹/min at risk.
- **FR-28 (new) ₹ Saved.** `saved = counterfactual_loss − actual_loss`, where counterfactual assumes the fault persists for a configurable manual MTTR (default 25 min) at the measured failure rate. Shown live on the top bar and in the postmortem; the assumption is always labelled.
- **FR-29** Postmortem draft: deterministic timeline + model-written narrative (≤ 300 words): cause, impact, detection/resolution times, twin prediction vs actual, action items.

### 9.7 Gemma showcase features
- **FR-30 (new) Voice war-room.** Push-to-talk in the UI records ≤ 30 s of audio; backend sends it to Gemma 4 E4B as native audio input along with the current incident context; the model may call read-only tools and answers in the language asked (English/Hindi/Hinglish). Answer is read aloud with the browser's built-in speech synthesis. Fallback: browser speech-to-text → text question to Gemma.
- **FR-31 (new) Bulletin reading.** `read_bulletin` passes the PNG to Gemma vision and returns extracted fields (bank, status, claimed window) as structured JSON for the verifier.
- **FR-32 (new) Ask-about-a-screenshot (stretch).** Drop any chart image into the agent panel and ask about it.

### 9.8 Demo reliability & credibility
- **FR-33 (new) Chaos Mode.** One button arms a hidden random scenario from {S1, S2, S3, S5} with random seed, offset and severity. The UI hides the scenario name until the agent finishes (or fails), then reveals ground truth with ✓/✗ and adds it to a session scoreboard.
- **FR-34** Replay mode: any recorded run replays through the same UI with original timing.
- **FR-35** Model warm-up at startup; health indicator; first call < 5 s.
- **FR-36** Model toggle: **Local E4B** (default) / **Large Gemma 4** remote (optional).

### 9.9 Evaluation harness
- **FR-37** Headless CLI runs each scenario N times with randomised seed, offset and severity, plus S0 runs, on fast-forward clock.
- **FR-38** Reports: root-cause accuracy per scenario, decoy cleared %, business-decline correctly not "fixed" %, false-incident rate, median/p90 time to verified RCA, mean tool calls, verifier rejection rate, twin prediction error (predicted vs actual recovery time).
- **FR-39 (new) Ablations:** verifier on vs off; E4B vs E2B (and remote large model if available). Results render on an in-app **Eval Board** and as `eval/results/<date>.md`.

## 10. Non-functional requirements

| Area | Target |
|---|---|
| Detection latency | ≤ 12 s simulated |
| Time to verified RCA (local E4B) | median ≤ 45 s, p90 ≤ 75 s |
| Fix Preview | ≤ 300 ms wall per action |
| Voice round-trip (release mic → spoken answer starts) | ≤ 8 s |
| Dashboard | 4 Hz chart, 60 fps UI, no layout shift |
| Event stream latency | ≤ 150 ms |
| Simulator | ≥ 300 txn/s real-time, < 15% of one core |
| Memory | backend ≤ 1.5 GB RSS; GPU within 6 GB VRAM |
| Offline | Full demo with network disconnected |
| Determinism | Same seed + scenario → identical telemetry hash |
| Accuracy | S1/S2/S3/S5 root cause ≥ 90%; S0 false incidents = 0 |
| Crash safety | Model/tool failure never crashes sim or UI; recoverable error state |

## 11. Success metrics (results slide)

1. Root-cause accuracy across ≥ 40 seeded runs (S1, S2, S3, S5).
2. Decoy cleared (% of S3 runs) and business surges correctly not remediated (% of S5).
3. Median time to verified diagnosis.
4. Verifier catches — wrong claims stopped before a human saw them — and accuracy with verifier off (the ablation).
5. Twin prediction error (seconds).
6. Chaos Mode live score during the demo.

## 12. Gemma 4 usage map (for the "Best Use of Gemma" judges)

| Gemma 4 capability | Where Proofline uses it | Why it's the right fit |
|---|---|---|
| Native function calling | The whole investigation loop, hypothesis updates, conclusion submission | Structured tool use is the agent's backbone |
| Small on-device model (E4B, 4-bit) | Everything runs locally on an RTX 3050 6 GB | Payments telemetry is sensitive; on-prem/offline is a real requirement |
| Native audio input (E2B/E4B) | Voice war-room | On-call engineers talk during incidents; no separate ASR model |
| Image input | Bank bulletin reading, screenshot questions | Real incidents involve screenshots and status pages |
| 140+ languages | Hindi/Hinglish voice answers and customer comms | India-first product |
| Thinking mode (optional) | Enabled only for the CONCLUDE step | Spend reasoning where it matters, keep tool loops fast |

## 13. The four phases

Each phase ends with a working, demoable product, passing tests and a commit. If time runs out at any phase boundary, you still have something to present. Suggested time share assumes ~24 build hours; scale proportionally.

### Phase 1 — Foundation: contract, simulator, live chart (~20%, H0–H5)
**Goal:** the "patient monitor" works and Codex is unblocked.

| Owner | Deliverables |
|---|---|
| Claude Code | **Full v2 contract defined up front** (including Phase 3–4 events, §15) so Codex never waits; JSON Schema + TS export; hand-authored fixtures for S1/S2/S3/S5; `make mock`; `SimClock`; vectorized simulator with decline codes; ring buffers + DuckDB; `metrics.tick` with technical/business split; S1 fault; Reset; **state snapshot/fork** primitives (needed later for the twin, cheap to design now) |
| Codex | App shell, theme tokens, WS client with resume/resync, Zustand reducer, HealthChart, BankTiles, KPIs, ControlBar — all against `make mock` |

**Exit criteria:** S1 visibly dips the chart and Reset restores it; determinism test passes; frontend renders every fixture event type; S0 runs 30 min with no false incident.

### Phase 2 — The agent that proves (~30%, H5–H12) → *minimum winnable demo*
**Goal:** S1 works end to end, verified and approved.

| Owner | Deliverables |
|---|---|
| Claude Code | Detector + change-point back-dating + decline breakdown; telemetry tools; model gateway (Ollama/llama.cpp, streaming, JSON repair, re-ask, circuit breaker, warm-up); orchestrator state machine; verifier (all v1 rules + `decline_class_matches`); blast radius; approval-token flow; remediation; scripted fake-model integration test |
| Codex | AgentStepper, HypothesisCards with transitions, ToolCallRow stream, VerifyBadge (rejections in amber), RcaCard, EvidenceDrawer, Approve/Reject, ErrorState |

**Exit criteria:** with the real Gemma 4 E4B, S1 goes detect → investigate → verify → approve → recover 3× in a row on the demo seed; median time to RCA ≤ 60 s.

### Phase 3 — Differentiators: hard cases, Fix Preview, Chaos, Gemma showcase (~30%, H12–H19)
**Goal:** the features that make judges remember the project. Build in this order (highest value per hour first):

1. **S3 decoy + S2 bank outage** (reuse S2 inside S3).
2. **Fix Preview** — fork, preview runs, `twin_predicts_recovery`, ghost line on chart, predicted-vs-actual label.
3. **S5 business-decline surge** + `notify_only` action + decline donut in UI.
4. **Chaos Mode** + session scoreboard.
5. **₹ Saved** counter.
6. **Voice war-room** — *spike first (30 min):* confirm E4B audio works in the chosen server (llama.cpp's multimodal path has added Gemma 4 audio support; stock Ollama support is unconfirmed). If it fails, ship the browser speech-to-text fallback and keep the feature.
7. **Bulletin image + `external_claim_consistent`.**

| Owner | Deliverables |
|---|---|
| Claude Code | Items 1–7 backend; new tools; new verifier rules; `/api/chaos`, `/api/voice`; fixtures for S3/S5/chaos/voice |
| Codex | Ghost prediction line, decline donut, Chaos button + reveal animation + scoreboard, ₹ Saved KPI, push-to-talk mic button with waveform, bulletin evidence view |

**Exit criteria:** S1, S2, S3, S5 each pass 3× in a row; Chaos Mode scores ≥ 4/5 in rehearsal; voice question answered in Hinglish within 8 s (or fallback path works).

### Phase 4 — Proof & polish: eval, comms, postmortem, hardening, rehearsal (~20%, H19–H24)
**Goal:** honest numbers, a story artifact, and a demo that cannot fail.

| Owner | Deliverables |
|---|---|
| Claude Code | Eval harness with ablations (verifier off, E2B vs E4B); Eval Board endpoint; postmortem generator; stakeholder comms drafts (EN + HI); recorder/player; regenerate fixtures from real passing runs; soak test, circuit breaker, error paths; demo seed locked |
| Codex | Eval Board view, Comms drafts panel, PostmortemModal, Projector theme check, hidden demo controls (`Shift+D`), Playwright smoke test |
| Both | Rehearse the §8 script 3×; record a backup video; test projector at the venue |

**Stretch (only if everything above is green):** skeptic pass (FR-18); incident memory (retrieve similar past postmortems); S4 traffic spike; S6 compound fault; ask-about-a-screenshot; **simulator-distilled fine-tune** — the simulator produces unlimited labelled investigation traces, which could LoRA-tune E2B to match E4B. High wow for the Gemma track but risky on a 6 GB GPU in a day; attempt only with time to spare.

**Exit criteria:** demo readiness checklist (§17) all green.

### Cut order if behind
Stretch items → ask-about-screenshot → comms drafts → bulletin image → voice (keep fallback text Q&A) → S2 standalone (keep inside S3) → postmortem. **Never cut:** verifier, S3 decoy, Fix Preview, Chaos Mode, eval numbers.

## 14. Feature → judging criteria map

| Likely criterion | Proofline's answer |
|---|---|
| Innovation | Three-gate pipeline (evidence, logic, outcome); fix preview via digital twin; verified external claims |
| Use of Gemma | Function calling + audio + vision + multilingual, all on-device on a 6 GB GPU |
| Technical depth | Deterministic vectorized simulator, change-point detection, forkable state, rules engine, event-sourced replay |
| Real-world impact | Technical vs business decline split mirrors how UPI success is tracked; ₹ saved |
| Demo quality | Chaos Mode blind test; replay fallback; projector theme |
| Honesty | Verifier rejections shown; `inconclusive` instead of guesses; ablation numbers |

## 15. Contract impact (v2)

Define all of these in Phase 1 (contract-first) so the frontend can build against fixtures immediately. Bump `contract_version` to **2**; record in `contracts/CHANGELOG.md`; update `design.md` §4.

**New / changed fields**
- `metrics.tick`: add `technical_success_rate`, `decline_mix: { technical_pct, business_pct }`.
- `incident.opened.incident`: add `decline_breakdown: { technical_pct, business_pct, top_codes: {code, pct}[] }`.
- `HypKind`: add `"business_decline"`. `ActionKind`: add `"notify_only"`. `AgentState`: add `"previewing"`, `"comms"`.
- `Evidence.kind` / `RCA.evidence[].kind`: add `"bulletin"`; `Evidence.body.bulletin?: { image_url, extracted: { bank, status, claimed_start_ts, claimed_end_ts } , consistent: boolean, detail: string }`.
- `action.proposed.action`: add `preview: { recovers, expected_recovery_ms, series: [ts, value][] }`.

**New events**
- `twin.preview { incident_id, action_id, kind, recovers, expected_recovery_ms, predicted: [ts, value][], baseline: [ts, value][] }`
- `impact.savings { incident_id, actual_loss_paise, counterfactual_loss_paise, saved_paise, assumed_manual_mttr_ms }`
- `comms.drafted { incident_id, drafts: { audience: "internal"|"merchant"|"customer", lang: "en"|"hi", text }[] }`
- `voice.query { query_id, transcript, lang }` · `voice.answer { query_id, text, lang, evidence_ids }`
- `chaos.armed { round }` · `chaos.revealed { round, scenario, ground_truth, agent_cause, correct }`

**New REST**
- `POST /api/chaos` → `{ok, round}` · `POST /api/voice` (multipart audio ≤ 30 s) → `{ok, query_id}`
- `POST /api/comms/approve { incident_id }` → `{ok}` · `GET /api/eval/latest` → eval table JSON

## 16. Risks

| Risk | Mitigation |
|---|---|
| E4B slow or malformed tool calls | Constant cached prefix, compact tool outputs, JSON repair + one re-ask, temperature 0.2, step cap |
| Gemma 4 audio not working in local server | 30-min spike at Phase 3 start; llama.cpp multimodal path as primary; browser speech-to-text fallback |
| VRAM pressure with audio/vision encoders | Load encoders only when needed; keep text-only path default; measure in Phase 1 |
| Model blames latest deploy | Decoy scenario + `deploy_precedes_dip` + required `ruled_out` list |
| Twin disagrees with reality | Same engine and RNG state; show predicted-vs-actual openly; eval measures error |
| Chaos Mode fails live | Rehearse ≥ 10 chaos rounds; restrict pool to scenarios passing ≥ 90% in eval |
| Venue wifi/projector | Offline local model; projector theme; replay mode; backup recording |
| Scope creep | Phase gates; cut order (§13); Phase 2 is a complete demo on its own |
| "Is this real?" | Say up front: traffic simulated, agent real; show eval board and chaos results |

## 17. Demo readiness checklist

- [ ] S1, S2, S3, S5 each pass 3× consecutively on demo seed
- [ ] Chaos Mode ≥ 9/10 in final rehearsal
- [ ] Fix Preview ghost line matches actual recovery within ±3 s
- [ ] Voice question (English + Hinglish) answered, or fallback works
- [ ] Reset returns exactly to healthy baseline
- [ ] Evidence chips open the correct records (deploy diff, logs, bulletin)
- [ ] Eval Board populated with ≥ 40 runs and the verifier-off ablation
- [ ] Replay of last good run works offline
- [ ] Model warmed; first call < 5 s
- [ ] Projector theme readable from the back row
- [ ] Backup recording saved locally and on phone

## 18. Open questions

- Exact event duration and presentation slot (tune §8 script and phase hours).
- Does the chosen local server support Gemma 4 E4B audio on the RTX 3050 within VRAM? (Phase 3 spike.)
- Access to a larger Gemma 4 model for the ablation (AI Studio vs cloud GPU), if network allows.
- Final project name (Proofline is the working name).

## 19. Research notes & sources

- Gemma 4 E2B/E4B accept text, image and audio input with native function calling; audio clips up to ~30 s; 140+ languages. — [Hugging Face: Gemma 4 E4B model card](https://huggingface.co/google/gemma-4-E4B), [HF blog: Welcome Gemma 4](https://huggingface.co/blog/gemma4), [Gemma 4 cheatsheet](https://cheatsheets.zip/gemma4)
- llama.cpp added a Gemma 4 audio conformer encoder to its multimodal path; earlier community reports flagged incomplete audio support, so verify on the target build. — [llama.cpp commit #21421](https://app.semanticdiff.com/gh/ngxson/llama.cpp/commit/547765a93e5ad7b4e8ca84d78f6d83f36ad8ee25), [Unsloth discussion](https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/discussions/1)
- AI SRE landscape (Datadog Bits AI SRE, incident.io, Resolve AI, Cleric, etc.): hypothesis-driven RCA is now table stakes; accuracy claims are largely vendor-reported with no recognised benchmark. — [Augment Code comparison](https://www.augmentcode.com/tools/best-ai-sre-tools), [Better Stack comparison](https://betterstack.com/community/comparisons/ai-sre-tools/), [Help Net Security on Bits AI SRE](https://www.helpnetsecurity.com/2025/12/03/datadog-bits-ai-sre-agent/)
- UPI technical vs business declines; technical declines ~0.7–0.8% by 2025; visibility into failure reasons as a gap. — [D91 Labs: Why UPI success rate matters](https://d91labs.substack.com/p/why-upi-success-rate-matters)
- Government push on UPI failures; NPCI reported downtime for 11 banks in Feb 2026. — [Outlook Money](https://www.outlookmoney.com/banking/upi-cleanup-drive-upi-glitches-under-scanner-as-govt-seeks-higher-success-rates)
