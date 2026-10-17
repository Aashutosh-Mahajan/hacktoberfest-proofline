# Proofline frontend

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
