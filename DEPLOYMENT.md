# Render backend + Vercel frontend

The frontend is https://frontend-zeta-olive-60.vercel.app. It calls the Render backend directly over HTTPS and WSS.

## 1. Fix the existing Render build

In the Render service's **Settings > Build & Deploy**, set:

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python -m uvicorn mayday.api.app:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health Check Path | `/api/health` |

The reported `requirements.txt` error means the build ran from the repository root. The file is at `backend/requirements.txt`.

If you keep Root Directory empty instead, use `pip install -r backend/requirements.txt` and `python -m uvicorn mayday.api.app:app --app-dir backend --host 0.0.0.0 --port $PORT --workers 1`.

Set these environment variables on Render:

| Variable | Value |
| --- | --- |
| `MAYDAY_ALLOWED_ORIGINS` | `https://frontend-zeta-olive-60.vercel.app` |
| `MAYDAY_MODEL_URL` | The base URL of your reachable Ollama server, without `/api/chat` |
| `MAYDAY_MODEL_NAME` | `gemma4:e4b` |
| `MAYDAY_RUNS_DIR` | `/tmp/proofline-runs` |

Comma-separated additional exact frontend origins can be added to `MAYDAY_ALLOWED_ORIGINS`.

`render.yaml` describes the same configuration for a new Render Blueprint. Committing it does not automatically change the settings of an existing manually created Render service.

## 2. Connect Vercel

Use the **Vite** framework, Root Directory `frontend`, Install Command `npm ci`, Build Command `npm run build`, and Output Directory `dist`. Enable access to source files outside the root directory if Vercel requests it: the frontend imports the shared `contracts/types.ts` file.

In Vercel's Environment Variables, set `VITE_BACKEND_URL` to your actual Render service origin, for example `https://your-service.onrender.com`. Do not add `/api` or `/ws`.

Redeploy the frontend after setting or changing that variable. Vite embeds it at build time. REST calls, audio uploads, bulletin images and WebSockets all use this origin. HTTPS automatically becomes WSS for `/ws`.

`frontend/vercel.json` supplies the Vite build settings when the Vercel Root Directory is `frontend`. No Vercel proxy is required; the browser connects directly to Render.

## 3. Connect Gemma

Render does not run the Ollama instance on your laptop. `http://localhost:11434` on Render means the Render container itself.

Run Gemma on a server reachable from the Render backend, or establish a protected connection to your existing model host. The endpoint must support Ollama's `/api/tags`, `/api/show` and `/api/chat` APIs and have `gemma4:e4b` installed. Do not place model credentials in `VITE_` variables, which are public browser configuration.

The API and simulator can start while the model is unavailable, but live Gemma investigations cannot work until the model endpoint is connected. The dashboard must show the model as unavailable in that situation.

## 4. Verify

1. Open `https://YOUR-RENDER-SERVICE.onrender.com/api/health`: check `ok`, then inspect `model.healthy` and `model.warm` separately.
2. Redeploy Vercel with `VITE_BACKEND_URL`, then open the frontend.
3. In the browser Network panel, confirm API requests go to Render and the `wss://YOUR-RENDER-SERVICE.onrender.com/ws` connection opens.
4. Start a traffic surge, inspect the preview, and approve the simulated fix.

This is a synthetic, single-process demo. Use one backend worker/instance because simulation and approval state live in memory. A restart resets that state; `/tmp` recordings are temporary.

References: [Render monorepos](https://render.com/docs/monorepo-support), [Render WebSockets](https://render.com/docs/websocket), [Vite environment variables](https://vite.dev/guide/env-and-mode).
