import type { ModelStatus } from "@contract";
import { api } from "./api";
import { useStore } from "../store/store";

const names = (raw: unknown): string[] | undefined =>
  Array.isArray(raw) ? raw.map((x) => (typeof x === "string" ? x : String((x as { name?: string })?.name ?? ""))).filter(Boolean) : undefined;

/** Poll /api/health quietly (no toast) so the model chip reflects loading / ready / unavailable. */
export async function refreshHealth(): Promise<void> {
  const r = await api.healthQuiet();
  if (!r || r.ok === false || !r.model) return;
  const choice = typeof r.active === "string" ? r.active : undefined;
  useStore.getState().setModelInfo(r.model as ModelStatus, names(r.models), choice);
}

export function startHealthPolling(ms = 3000): () => void {
  void refreshHealth();
  const id = window.setInterval(() => void refreshHealth(), ms);
  return () => window.clearInterval(id);
}

/** Switch model (POST /api/model) then refresh the chip. Only offered when /api/health lists several models. */
export async function switchModel(name: string): Promise<boolean> {
  useStore.getState().setSwitching(true);
  const r = await api.setModel(name);
  if (r.ok) { useStore.setState({ modelChoice: name }); await refreshHealth(); }
  useStore.getState().setSwitching(false);
  return !!r.ok;
}

export type ModelView = { name: string; state: "loading" | "ready" | "unavailable"; label: string; tone: "live" | "warn" | "bad" };
export function describeModel(m: ModelStatus): ModelView {
  const name = m.name && m.name !== "—" ? m.name : "Gemma";
  if (!m.healthy) return { name, state: "unavailable", label: "unavailable", tone: "bad" };
  if (!m.warm) return { name, state: "loading", label: "loading", tone: "warn" };
  return { name, state: "ready", label: "ready", tone: "live" };
}
