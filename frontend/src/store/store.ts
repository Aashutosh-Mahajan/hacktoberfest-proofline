import { create } from "zustand";
import type { Envelope } from "@contract";
import { initialState, reduce } from "./reducer";
import type { AppState, ConnState } from "./types";

export interface Actions {
  dispatch: (env: Envelope) => void;
  dispatchBatch: (envs: Envelope[]) => void;
  setConn: (c: ConnState) => void;
  markProposal: (status: "approving" | "rejected" | "pending") => void;
  markCommsApproved: () => void;
  addVoicePending: (query_id: string, transcript?: string) => void;
  localReset: () => void;
  pushError: (code: string, message: string, recoverable?: boolean) => void;
  dismissError: (id: number) => void;
}

export const useStore = create<AppState & Actions>((set) => ({
  ...initialState(),
  dispatch: (env) => set((s) => reduce(s, env)),
  dispatchBatch: (envs) => set((s) => envs.reduce<AppState>((acc, e) => reduce(acc, e), s)),
  setConn: (conn) => set({ conn }),
  markProposal: (status) => set((s) => (s.proposal ? { proposal: { ...s.proposal, status } } : {})),
  markCommsApproved: () => set({ commsApproved: true }),
  addVoicePending: (query_id, transcript) =>
    set((s) =>
      s.voice.some((v) => v.query_id === query_id)
        ? {}
        : { voice: [...s.voice, { query_id, transcript, evidence_ids: [], pending: true, ts: Date.now() }] },
    ),
  localReset: () =>
    set((s) => {
      const fresh = initialState();
      return { ...fresh, conn: s.conn, lastSeq: s.lastSeq, model: s.model, chaos: { ...fresh.chaos, scoreboard: s.chaos.scoreboard }, voice: s.voice };
    }),
  pushError: (code, message, recoverable = true) =>
    set((s) => ({ errors: [...s.errors.slice(-9), { id: s.nextId, code, message, recoverable, ts: Date.now() }], nextId: s.nextId + 1 })),
  dismissError: (id) => set((s) => ({ errors: s.errors.filter((e) => e.id !== id) })),
}));
