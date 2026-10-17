# ProofLine frontend

Vite + React 18 + TypeScript (strict) + Zustand + uPlot. Wire contract: `../contracts/types.ts` (aliased as `@contract`, never edited here).

## Run
```
cd frontend && npm install
npm run dev            # http://localhost:5173, proxies /api and /ws to :8000 (MAYDAY_BACKEND overrides)
npm run dev  -> open  /?mock=1[&speed=2]   # fully in-browser simulator (S1, S2, S3, S5, chaos, voice Q&A); no backend needed
npm run build          # tsc --noEmit + vite build
npm test               # vitest (reducer covers every EventMap event)
```
## Layout
- `src/store/reducer.ts` pure reducer for every contract event (unknown events ignored, duplicate seq dropped, snapshot resync); `store.ts` Zustand wrapper.
- `src/net/wsClient.ts` WS with `?since=<seq>` resume, backoff reconnect, frame-batched dispatch. `api.ts` REST helpers (swappable transport for the mock).
- `src/dev/mockServer.ts` in-browser event simulator behind `?mock=1`.
- `src/components/` TopBar, ControlBar, HealthChart (uPlot: per-bank lines, incident shading, deploy markers, ghost twin forecast), BankTiles, DeclineDonut, Agent (stepper, hypotheses, verifier, tool stream), RcaCard, ProposedActionCard (preview, approve/reject, predicted vs actual), EvidenceDrawer, Chaos reveal, ErrorState, demo panel.
- `src/panels/` Eval board, comms drafts, postmortem modal, voice war-room, replay controls.
- Themes: dark mission-control (default) and high-contrast projector (toggle in the top bar, persisted). `Shift+D` opens hidden demo controls.
- Rates may arrive as fractions or percents; `lib/format.ts` normalises both.


## Redesign and performance notes
- Plain-language UI: vocabulary lives in `src/lib/words.ts` (technical terms stay as tooltips). Views: Live (health hero, suggested fix, "What the AI is doing"), Banks and Ask; scenario controls, replay and chaos live in the **Simulate** popover.
- Backend rates are PERCENT. A snapshot fully resets the store; events replayed after it (seq below the snapshot seq) are applied; `pending_action` restores the Approve button after a reconnect. The WS client always connects with `since=0`.
- 4 Hz metrics are kept in `src/store/chartBuffer.ts` (outside React); uPlot is lazy-loaded and repainted once per frame. KPIs and tiles re-render at most 2 Hz (`useThrottled`). Rarely used views are code-split.
- `npm run demo` serves the production build on :5173 with the same `/api` and `/ws` proxy (`MAYDAY_BACKEND` overrides the target, `MAYDAY_PORT` the port).
- `npm run test:e2e` runs a Playwright smoke test against a real backend (default `http://localhost:8003`; start it with `python -m uvicorn mayday.api.app:app --port 8003` from `backend`).

## Vercel + Render

Set `VITE_BACKEND_URL` in Vercel to your Render backend origin and redeploy. API calls and uploads use HTTPS; the live connection uses WSS at the same host. Leave it empty for the local Vite proxy. See [the deployment guide](../DEPLOYMENT.md) for Render root-directory settings, CORS and Gemma hosting.
