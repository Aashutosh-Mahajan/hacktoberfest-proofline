import type { Envelope, EventMap } from "@contract";

/**
 * 4 Hz metric history kept OUTSIDE React/Zustand state so a tick never re-renders anything; the chart subscribes
 * directly and repaints once per animation frame. Values are percent (0-100), x is seconds.
 */
export const CHART_CAP = 600; // 2.5 minutes @ 4 Hz

export interface ChartSeries { x: number[]; overall: number[]; X: number[]; Y: number[]; Z: number[] }

const empty = (): ChartSeries => ({ x: [], overall: [], X: [], Y: [], Z: [] });

class ChartBuffer {
  data: ChartSeries = empty();
  version = 0;
  private snapSeq = 0;
  private sourceTs = 0;
  private offsetMs = 0;
  private listeners = new Set<() => void>();

  subscribe(fn: () => void) { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; }
  private bump() { this.version++; this.listeners.forEach((l) => l()); }

  /** A new socket is about to replay the ring: accept its head snapshot even if its seq is lower (server restart). */
  beginResync() { this.snapSeq = 0; }

  reset() { this.data = empty(); this.snapSeq = 0; this.sourceTs = 0; this.offsetMs = 0; this.bump(); }

  /** Keep the display clock moving forward when a new simulation rewinds its own clock. */
  private syncClock(ts: number) {
    if (this.data.x.length && ts < this.sourceTs) {
      this.offsetMs = this.data.x[this.data.x.length - 1] * 1000 - ts;
      this.sourceTs = ts;
    }
  }

  chartTime(tsMs: number) { return (tsMs + this.offsetMs) / 1000; }
  get timeOffsetMs() { return this.offsetMs; }

  private push(tsMs: number, m: EventMap["metrics.tick"]): void {
    const d = this.data;
    let t = this.chartTime(tsMs);
    const n = d.x.length;
    if (n && t <= d.x[n - 1]) {
      if (tsMs >= this.sourceTs - 5000) return;
      // A rewind without a preceding status/snapshot still starts after the last plotted sample.
      this.offsetMs = d.x[n - 1] * 1000 + 250 - tsMs;
      t = this.chartTime(tsMs);
    }
    this.sourceTs = tsMs;
    d.x.push(t); d.overall.push(m.success_rate);
    d.X.push(m.by_bank?.BANK_X?.success_rate ?? NaN); d.Y.push(m.by_bank?.BANK_Y?.success_rate ?? NaN); d.Z.push(m.by_bank?.BANK_Z?.success_rate ?? NaN);
    if (d.x.length > CHART_CAP + 60) { // amortised trim: one slice per 60 ticks
      const cut = d.x.length - CHART_CAP;
      d.x = d.x.slice(cut); d.overall = d.overall.slice(cut); d.X = d.X.slice(cut); d.Y = d.Y.slice(cut); d.Z = d.Z.slice(cut);
    }
  }

  /** Feed a whole batch of envelopes (one notification at the end). */
  feed(envs: Envelope[]) {
    let changed = false;
    for (const env of envs) {
      if (env.type === "snapshot") {
        if (this.snapSeq > 0 && env.seq <= this.snapSeq) continue;
        const x = env.data as EventMap["snapshot"];
        const recent = x.recent_metrics ?? [];
        const clock = x.sim?.sim_ms ?? recent[recent.length - 1]?.ts;
        if (clock != null) this.syncClock(clock);
        this.snapSeq = env.seq;
        for (const r of recent) {
          // Snapshots overlap existing history. Merge only new samples rather than clearing the graph.
          if (!this.data.x.length || r.ts > this.sourceTs) this.push(r.ts, r.m);
        }
        changed = true;
      } else if (env.type === "sim.status") {
        if (this.snapSeq > 0 && env.seq <= this.snapSeq) continue;
        this.syncClock((env.data as EventMap["sim.status"]).sim_ms);
      } else if (env.type === "metrics.tick") {
        if (this.snapSeq > 0 && env.seq <= this.snapSeq) continue; // replayed history is already in the snapshot
        this.push(env.ts, env.data as EventMap["metrics.tick"]);
        changed = true;
      }
    }
    if (changed) this.bump();
  }
}

export const chartBuffer = new ChartBuffer();

/** Merge the live series with the 1 Hz twin series onto one x axis (union of timestamps). */
export function buildChartData(
  s: ChartSeries,
  preview?: { predicted: [number, number][]; baseline: [number, number][] },
  offsetMs = 0,
): (number | null)[][] {
  const n = s.x.length;
  const hasPrev = !!preview && (preview.predicted.length > 0 || preview.baseline.length > 0);
  if (!hasPrev) {
    const none = new Array<number | null>(n).fill(null);
    return [s.x, s.overall, s.X, s.Y, s.Z, none, none];
  }
  const time = (ts: number) => (ts + offsetMs) / 1000;
  const xs = [...new Set<number>([...s.x, ...preview!.predicted.map((p) => time(p[0])), ...preview!.baseline.map((p) => time(p[0]))])].sort((a, b) => a - b);
  const idx = new Map<number, number>();
  xs.forEach((t, i) => idx.set(t, i));
  const align = (vals: number[]) => {
    const out = new Array<number | null>(xs.length).fill(null);
    s.x.forEach((t, i) => { out[idx.get(t)!] = vals[i]; });
    return out;
  };
  const place = (pts: [number, number][]) => {
    const out = new Array<number | null>(xs.length).fill(null);
    for (const [t, v] of pts) out[idx.get(time(t))!] = v;
    return out;
  };
  return [xs, align(s.overall), align(s.X), align(s.Y), align(s.Z), place(preview!.predicted), place(preview!.baseline)];
}
