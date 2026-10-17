import { describe, expect, it } from "vitest";
import type { Envelope, MetricsTick } from "@contract";
import { chartBuffer, buildChartData, CHART_CAP } from "./chartBuffer";

const m = (sr: number): MetricsTick => ({
  success_rate: sr, technical_success_rate: sr, decline_mix: { technical_pct: 50, business_pct: 50 }, tps: 1, p95_ms: 1, paise_failed_per_min: 0,
  by_bank: { BANK_X: { success_rate: sr, p95_ms: 1, tps: 1 }, BANK_Y: { success_rate: 99, p95_ms: 1, tps: 1 }, BANK_Z: { success_rate: 98, p95_ms: 1, tps: 1 } },
});
const tick = (seq: number, ts: number, sr = 99): Envelope => ({ v: 2, seq, ts, type: "metrics.tick", data: m(sr) });
const snap = (seq: number, recent: { ts: number; m: MetricsTick }[]): Envelope => ({ v: 2, seq, ts: 0, type: "snapshot", data: { recent_metrics: recent } as never });

describe("chartBuffer", () => {
  it("keeps percent values and skips replayed ticks after a snapshot", () => {
    chartBuffer.reset();
    chartBuffer.feed([snap(10, [{ ts: 5000, m: m(98.5) }]), tick(3, 4000), tick(11, 5250, 97)]);
    expect(chartBuffer.data.x).toEqual([5, 5.25]);
    expect(chartBuffer.data.overall).toEqual([98.5, 97]);
  });
  it("continues when the sim clock jumps back and caps its size", () => {
    chartBuffer.reset();
    chartBuffer.feed([tick(1, 100_000), tick(2, 1_000)]);
    expect(chartBuffer.data.x).toEqual([100, 100.25]);
    chartBuffer.reset();
    const many: Envelope[] = [];
    for (let i = 0; i < CHART_CAP + 200; i++) many.push(tick(i + 1, 1_000 + i * 250));
    chartBuffer.feed(many);
    expect(chartBuffer.data.x.length).toBeLessThanOrEqual(CHART_CAP + 60);
  });
  it("merges reconnect snapshots without losing older samples or duplicating overlap", () => {
    chartBuffer.reset();
    chartBuffer.feed([tick(1, 1000, 97), tick(2, 1250, 98)]);
    chartBuffer.beginResync();
    chartBuffer.feed([snap(10, [{ ts: 1250, m: m(98) }, { ts: 1500, m: m(99) }]), tick(3, 1250)]);
    expect(chartBuffer.data.x).toEqual([1, 1.25, 1.5]);
    expect(chartBuffer.data.overall).toEqual([97, 98, 99]);
    chartBuffer.feed([snap(11, [])]);
    expect(chartBuffer.data.x).toEqual([1, 1.25, 1.5]);
  });
  it("keeps history through a scenario reset and aligns forecasts and markers to the continuing clock", () => {
    chartBuffer.reset();
    chartBuffer.feed([tick(1, 100_000, 90)]);
    const status: Envelope = { v: 2, seq: 2, ts: 1000, type: "sim.status", data: { sim_ms: 1000 } as never };
    chartBuffer.feed([status, { ...snap(3, []), data: { sim: { sim_ms: 1000 }, recent_metrics: [] } as never }, tick(4, 1250, 99)]);
    expect(chartBuffer.data.x).toEqual([100, 100.25]);
    expect(chartBuffer.data.overall).toEqual([90, 99]);
    expect(chartBuffer.chartTime(1250)).toBe(100.25);
    const data = buildChartData(chartBuffer.data, { predicted: [[1250, 99], [2250, 100]], baseline: [] }, chartBuffer.timeOffsetMs);
    expect(data[0]).toEqual([100, 100.25, 101.25]);
    expect(data[5]).toEqual([null, 99, 100]);
    // Repeated reconnects keep the same offset rather than shifting the timeline again.
    chartBuffer.beginResync();
    chartBuffer.feed([{ ...snap(5, [{ ts: 1250, m: m(99) }]), data: { sim: { sim_ms: 1250 }, recent_metrics: [{ ts: 1250, m: m(99) }] } as never }, tick(6, 1500)]);
    expect(chartBuffer.data.x).toEqual([100, 100.25, 100.5]);
  });
  it("notifies once per batch", () => {
    chartBuffer.reset();
    let n = 0;
    const off = chartBuffer.subscribe(() => n++);
    chartBuffer.feed([tick(1, 1000), tick(2, 1250), tick(3, 1500)]);
    off();
    expect(n).toBe(1);
  });
  it("merges the 1 Hz ghost onto the x axis so it is a connected series", () => {
    chartBuffer.reset();
    chartBuffer.feed([tick(1, 1000), tick(2, 1250), tick(3, 1500)]);
    const d = buildChartData(chartBuffer.data, { predicted: [[1500, 90], [2500, 99]], baseline: [[1500, 88]] });
    expect(d[0]).toEqual([1, 1.25, 1.5, 2.5]);
    expect(d[5]).toEqual([null, null, 90, 99]);
    expect(d[6]).toEqual([null, null, 88, null]);
    expect(d[1]).toEqual([99, 99, 99, null]);
  });
});
