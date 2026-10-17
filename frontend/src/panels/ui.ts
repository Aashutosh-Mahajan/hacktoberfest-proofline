import type { CSSProperties } from "react";
import { backendUrl } from "../net/endpoints";

const v = (name: string, fb: string) => `var(${name}, ${fb})`;
export const C = {
  bg: v("--bg", "#0b0f14"),
  panel: v("--panel", "#121821"),
  fg: v("--fg", "#e6edf3"),
  muted: v("--muted", "#8b98a8"),
  accent: v("--accent", "#4da3ff"),
  warn: v("--warn", "#f0b429"),
  ok: v("--ok", "#3fb950"),
  border: v("--border", "#243040"),
};

export const box: CSSProperties = {
  background: C.panel, color: C.fg, border: `1px solid ${C.border}`, borderRadius: 8,
  padding: 12, fontSize: 13, fontFamily: "inherit",
};
export const btn: CSSProperties = {
  background: C.accent, color: "#04111f", border: "none", borderRadius: 6, padding: "6px 12px",
  fontWeight: 600, cursor: "pointer", fontSize: 13,
};
export const btnGhost: CSSProperties = { ...btn, background: "transparent", color: C.fg, border: `1px solid ${C.border}` };
export const input: CSSProperties = {
  background: C.bg, color: C.fg, border: `1px solid ${C.border}`, borderRadius: 6, padding: "6px 8px", fontSize: 13, flex: 1, minWidth: 0,
};
export const h3: CSSProperties = { margin: "0 0 8px", fontSize: 14, fontWeight: 600 };

export async function jsonCall(method: string, path: string, body?: unknown): Promise<{ ok: boolean; error?: string; [k: string]: unknown }> {
  try {
    const res = await fetch(backendUrl(path), {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok && j.ok !== false) return { ok: false, error: "HTTP " + res.status };
    return j;
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
