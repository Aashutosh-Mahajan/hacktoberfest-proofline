# Proofline — Design (v2)

See `prd.md` for product requirements. This document fixes architecture and ownership so work can run in parallel.

## Stack
- **Backend**: Python 3.11+, FastAPI + uvicorn (REST + WebSocket), numpy (vectorized sim), pydantic. Package `backend/mayday`.
- **Model**: Gemma 4 via an OpenAI-compatible/Ollama endpoint (`MAYDAY_MODEL_URL`, default `http://localhost:11434`). A deterministic **scripted model** (`backend/mayday/agent/gateway.py`) is the offline/test fallback so the whole product runs and is testable without a GPU. The orchestrator/verifier are model-agnostic.
- **Frontend**: Vite + React 18 + TypeScript + Zustand + uPlot. Dev proxy to `localhost:8000`.
- **Contract**: `contracts/types.ts` is the single source of truth (WS envelope + events + REST). `contracts/CHANGELOG.md` tracks versions.

## Layout and ownership
```
contracts/types.ts            contract (frozen for this build; additive changes only, log in CHANGELOG)
backend/mayday/core/          protocols.py (Telemetry/World/EventBus), bus.py (event bus + ring replay), config.py
backend/mayday/sim/           [OWNER: sim]   engine, scenarios, telemetry impl, fork/twin, detector, bulletin PNG, savings math
backend/mayday/agent/         [OWNER: agent] tools, verifier, gateway, orchestrator, comms, postmortem, voice
backend/mayday/api/           [OWNER: sim]   FastAPI app, WS, REST, wiring of sim+agent; fixtures mock server (`make mock`)
backend/tests/                pytest, per owner
eval/                         [OWNER: eval, later] headless harness, ablations, results/*.md
frontend/                     [OWNER: frontend] full UI
```
The agent layer talks to the world only through `core/protocols.py`. The api layer constructs `Orchestrator(world, bus, gateway)` from `mayday.agent` (interface: `Orchestrator.on_incident(incident: dict)`, `.approve(incident_id, action_id, token) -> dict`, `.reject(...)`, `.ask(text, lang) -> query_id`, `.snapshot() -> dict`) and `ScriptedGateway` / `OllamaGateway`.

## Pipeline (three gates)
detector opens incident → TRIAGE (decline breakdown, blast radius in code) → INVESTIGATE (≤10 tool calls, hypothesis board) → CONCLUDE → VERIFY (deterministic rules; ≤3 attempts; rejections streamed) → PREVIEW (fork twin; must recover) → PROPOSE (single-use 5-min approval token) → human approve → REMEDIATE (idempotent) → compare predicted vs actual → COMMS (EN+HI drafts) → POSTMORTEM → ₹ saved.

## Verifier rules
evidence_exists, deploy_precedes_dip, deploy_on_failing_path, scope_concentration, others_healthy, no_internal_cause, traffic_elevated, action_matches_cause, decline_class_matches, external_claim_consistent, twin_predicts_recovery. Model proposes; code decides.

## Determinism
Everything derives from `(seed, scenario, fault_offset_s, severity)` via `numpy.random.Generator` with explicit state; sim clock is decoupled from wall time. Same inputs → identical telemetry hash.

## Run
`make dev` (backend :8000 + frontend :5173), `make test`, `make eval`, `make mock` (fixture server, no sim).
