import { create } from "zustand";

/** Local UI state (not wire state). */
export const useUi = create<{
  evidenceId: string | null; openEvidence: (id: string) => void; closeEvidence: () => void;
  demo: boolean; toggleDemo: () => void;
  showPostmortem: boolean; setPostmortem: (v: boolean) => void;
}>((set) => ({
  evidenceId: null, openEvidence: (id) => set({ evidenceId: id }), closeEvidence: () => set({ evidenceId: null }),
  demo: false, toggleDemo: () => set((s) => ({ demo: !s.demo })),
  showPostmortem: false, setPostmortem: (v) => set({ showPostmortem: v }),
}));
