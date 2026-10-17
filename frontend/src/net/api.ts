import type { Evidence, EvalReport, Scenario } from "@contract";
import { useStore } from "../store/store";

/** Transport can be swapped for the in-browser mock (?mock=1). */
export interface Transport {
  request: (method: string, path: string, body?: unknown) => Promise<unknown>;
  upload?: (path: string, form: FormData) => Promise<unknown>;
}

let transport: Transport = {
  async request(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let json: unknown = {};
    try { json = text ? JSON.parse(text) : {}; } catch { json = { ok: false, error: text.slice(0, 200) }; }
    if (!res.ok && (json as { ok?: boolean }).ok !== false) return { ok: false, error: "HTTP " + res.status };
    return json;
  },
  async upload(path, form) {
    const res = await fetch(path, { method: "POST", body: form });
    return res.json();
  },
};

export const setTransport = (t: Transport) => { transport = t; };

export interface ApiResult { ok: boolean; error?: string; [k: string]: unknown }

async function call(method: string, path: string, body?: unknown): Promise<ApiResult> {
  try {
    const r = (await transport.request(method, path, body)) as ApiResult;
    if (r && r.ok === false) useStore.getState().pushError("api_error", method + " " + path + ": " + (r.error ?? "request failed"), true);
    return r ?? { ok: false, error: "empty response" };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    useStore.getState().pushError("network", "Cannot reach backend (" + path + "): " + msg, true);
    return { ok: false, error: msg };
  }
}

export const api = {
  health: () => call("GET", "/api/health"),
  state: () => call("GET", "/api/state"),
  scenario: (scenario: Scenario, severity?: number, fault_offset_s?: number, seed?: number) =>
    call("POST", "/api/scenario", { scenario, severity, fault_offset_s, seed }),
  reset: async () => { const r = await call("POST", "/api/reset"); if (r.ok) useStore.getState().localReset(); return r; },
  approve: (incident_id: string, action_id: string, token: string) => call("POST", "/api/approve", { incident_id, action_id, token }),
  reject: (incident_id: string, action_id: string) => call("POST", "/api/reject", { incident_id, action_id }),
  chaos: () => call("POST", "/api/chaos"),
  ask: (text: string, lang?: string) => call("POST", "/api/ask", { text, lang }),
  approveComms: (incident_id: string) => call("POST", "/api/comms/approve", { incident_id }),
  replayList: () => call("GET", "/api/replay/list"),
  replayStart: (run_id: string) => call("POST", "/api/replay/start", { run_id }),
  replayStop: () => call("POST", "/api/replay/stop"),
  async voice(blob: Blob): Promise<ApiResult> {
    const form = new FormData();
    form.append("audio", blob, "voice.webm");
    try {
      if (transport.upload) return (await transport.upload("/api/voice", form)) as ApiResult;
      return { ok: false, error: "upload unsupported" };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      useStore.getState().pushError("voice_upload", "Voice upload failed: " + msg, true);
      return { ok: false, error: msg };
    }
  },
  async evidence(id: string): Promise<Evidence | null> {
    try {
      const r = (await transport.request("GET", "/api/evidence/" + encodeURIComponent(id))) as Evidence & { ok?: boolean };
      if (!r || r.ok === false) return null;
      return r;
    } catch { return null; }
  },
  async evalLatest(): Promise<EvalReport | null> {
    try {
      const r = (await transport.request("GET", "/api/eval/latest")) as EvalReport & { ok?: boolean };
      if (!r || r.ok === false || !Array.isArray(r.rows)) return null;
      return r;
    } catch { return null; }
  },
};
