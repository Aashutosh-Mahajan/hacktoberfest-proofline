import { create } from "zustand";

export type Theme = "dark" | "projector";

export const useTheme = create<{ theme: Theme; toggle: () => void }>((set, get) => ({
  theme: (document.documentElement.dataset.theme === "projector" ? "projector" : "dark") as Theme,
  toggle: () => {
    const next: Theme = get().theme === "dark" ? "projector" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("mayday.theme", next); } catch { /* storage unavailable */ }
    set({ theme: next });
  },
}));
