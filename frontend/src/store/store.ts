import { create } from "zustand";
import type { Envelope, ModelStatus } from "@contract";
import { initialState, reduce } from "./reducer";
import { chartBuffer } from "./chartBuffer";
import type { AppState, ConnState } from "./types";

export interface Actions {
  dispatch: (env: Envelope) => void;
  dispatchBatch: (envs: Envelope[]) => void;
  setConn: (c: ConnState) => void;
  beginResync: () => void;
  markProposal: (status: "approving" | "rejected" | "pending") => void;
  markCommsApproved: () => void;
  setFollowupBusy: (kind: string, on: boolean) => void;
  addVoicePending: (query_id: string, transcript?: string) => void;
  localReset: () => void;
  setModelInfo: (model: ModelStatus, models?: string[], choice?: string | null) => void;
  setSwitching: (v: boolean) => void;
  pushError: (code: string, message: string, recoverable?: boolean) => void;
  dismissError: (id: number) => void;
}

export const useStore = create<AppState & Actions>((set) => ({
  ...initialState(),
  dispatch: (env) => { chartBuffer.feed([env]); set((s) => reduce(s, env)); },
  // one state update (and one chart notification) per batch, however large the replayed backlog is
  dispatchBatch: (envs) => { chartBuffer.feed(envs); set((s) => envs.reduce<AppState>((acc, e) => reduce(acc, e), s)); },
  setConn: (conn) => set({ conn }),
  beginResync: () => { chartBuffer.beginResync(); set({ snapSeq: 0, lastSeq: 0 }); },
  markProposal: (status) => set((s) => (s.proposal ? { proposal: { ...s.proposal, status } } : {})),
  markCommsApproved: () => set({ commsApproved: true }),
  setFollowupBusy: (kind, on) => set((s) => ({ followupBusy: { ...s.followupBusy, [kind]: on } })),
  addVoicePending: (query_id, transcript) =>
    set((s) =>
      s.voice.some((v) => v.query_id === query_id)
        ? {}
        : { voice: [...s.voice, { query_id, transcript, evidence_ids: [], pending: true, ts: Date.now() }] },
    ),
  localReset: () =>
    set((s) => {
      const fresh = initialState();
      return { ...fresh, conn: s.conn, lastSeq: s.lastSeq, snapSeq: s.snapSeq, model: s.model, models: s.models, modelChoice: s.modelChoice, modelSwitching: s.modelSwitching, chaos: { ...fresh.chaos, scoreboard: s.chaos.scoreboard }, voice: s.voice };
    }),
  setModelInfo: (model, models, choice) => set((s) => ({ model, models: models ?? s.models, modelChoice: choice ?? s.modelChoice })),
  setSwitching: (modelSwitching) => set({ modelSwitching }),
  pushError: (code, message, recoverable = true) =>
    set((s) => s.errors.some((e) => e.code === code && e.message === message) ? {} : ({ errors: [...s.errors.slice(-9), { id: s.nextId, code, message, recoverable, ts: Date.now() }], nextId: s.nextId + 1 })),
  dismissError: (id) => set((s) => ({ errors: s.errors.filter((e) => e.id !== id) })),
}));
