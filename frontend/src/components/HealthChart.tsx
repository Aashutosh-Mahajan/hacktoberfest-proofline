import { useEffect, useMemo, useRef } from "react";
import uPlot from "uplot";
import { useStore } from "../store/store";
import { pct, fmtClock } from "../lib/format";
import { useTheme } from "../lib/theme";

const WINDOW_MS = 150_000;

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Merge predicted/baseline twin series onto the x axis. Returns x (seconds) and aligned arrays. */
export function buildChartData(
  s: { ts: number[]; overall: number[]; X: number[]; Y: number[]; Z: number[] },
  preview?: { predicted: [number, number][]; baseline: [number, number][] },
): (number | null)[][] {
  const n = s.ts.length;
  const lastTs = n ? s.ts[n - 1] : 0;
  const extra = new Set<number>();
  for (const [t] of preview?.predicted ?? []) if (t > lastTs) extra.add(t);
  for (const [t] of preview?.baseline ?? []) if (t > lastTs) extra.add(t);
  const ex = [...extra].sort((a, b) => a - b);
  const xs = [...s.ts, ...ex];
  const pad = <T,>(a: T[]) => [...a, ...ex.map(() => null)] as (T | null)[];
  const ghost = new Array<number | null>(xs.length).fill(null);
  const base = new Array<number | null>(xs.length).fill(null);
  const place = (arr: (number | null)[], pts: [number, number][]) => {
    for (const [t, v] of pts) {
      if (t > lastTs) arr[n + ex.indexOf(t)] = pct(v);
      else {
        // nearest existing index at or after t
        let lo = 0, hi = n - 1;
        while (lo < hi) { const m = (lo + hi) >> 1; if (s.ts[m] < t) lo = m + 1; else hi = m; }
        if (n && Math.abs(s.ts[lo] - t) < 600) arr[lo] = pct(v);
      }
    }
  };
  if (preview) { place(ghost, preview.predicted); place(base, preview.baseline); }
  return [xs.map((t) => t / 1000), pad(s.overall), pad(s.X), pad(s.Y), pad(s.Z), ghost, base];
}

export function HealthChart() {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const meta = useRef({ incStart: 0, incEnd: 0 as number | undefined, deploys: [] as { ts: number; n: number }[] });
  const series = useStore((s) => s.series);
  const preview = useStore((s) => s.preview);
  const incident = useStore((s) => s.incident);
  const deploys = useStore((s) => s.deploys);
  const theme = useTheme((s) => s.theme);

  const data = useMemo(() => buildChartData(series, preview), [series, preview]);

  meta.current.incStart = incident?.start_ts ?? 0;
  meta.current.incEnd = incident?.end_ts;
  meta.current.deploys = deploys.map((d) => ({ ts: d.ts, n: d.number }));

  useEffect(() => {
    const el = host.current!;
    const lw = parseFloat(css("--lw")) || 1.5;
    const mk = (label: string, color: string, width = lw, dash?: number[]): uPlot.Series => ({
      label, stroke: color, width, dash, spanGaps: false, points: { show: false },
    });
    const opts: uPlot.Options = {
      width: el.clientWidth, height: el.clientHeight, padding: [8, 8, 0, 0],
      cursor: { show: false }, legend: { show: false },
      scales: { x: { time: false }, y: { range: (_u, min, max) => [Math.max(0, Math.min(min, 90) - 4), Math.max(100, max + 0.5)] } },
      axes: [
        { space: 90, stroke: css("--dim"), grid: { stroke: css("--line"), width: 0.5 }, ticks: { show: false }, values: (_u, v) => v.map((x) => fmtClock(x * 1000)), font: "11px JetBrains Mono" },
        { stroke: css("--dim"), grid: { stroke: css("--line"), width: 0.5 }, ticks: { show: false }, size: 44, values: (_u, v) => v.map((x) => x + "%"), font: "11px JetBrains Mono" },
      ],
      series: [
        {},
        mk("Overall", css("--overall"), lw * 1.8), mk("BANK_X", css("--bx")), mk("BANK_Y", css("--by")), mk("BANK_Z", css("--bz")),
        mk("Predicted recovery", css("--twin"), lw * 1.8, [6, 4]), mk("Without action", css("--alert"), lw, [2, 4]),
      ],
      hooks: {
        drawClear: [(u) => {
          const { ctx } = u; const { left, top, width, height } = u.bbox;
          const m = meta.current;
          if (m.incStart) {
            const x0 = u.valToPos(m.incStart / 1000, "x", true);
            const x1 = m.incEnd ? u.valToPos(m.incEnd / 1000, "x", true) : left + width;
            ctx.fillStyle = css("--shade");
            ctx.fillRect(Math.max(left, x0), top, Math.max(0, Math.min(left + width, x1) - Math.max(left, x0)), height);
          }
        }],
        draw: [(u) => {
          const { ctx } = u; const { left, top, width, height } = u.bbox;
          ctx.save(); ctx.font = "11px JetBrains Mono"; ctx.strokeStyle = css("--warn"); ctx.fillStyle = css("--warn"); ctx.setLineDash([4, 4]);
          for (const d of meta.current.deploys) {
            const x = u.valToPos(d.ts / 1000, "x", true);
            if (x < left || x > left + width) continue;
            ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
            ctx.fillText("#" + d.n, x + 3, top + 12);
          }
          ctx.restore();
        }],
      },
    };
    const u = new uPlot(opts, data as uPlot.AlignedData, el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: el.clientHeight }));
    ro.observe(el);
    return () => { ro.disconnect(); u.destroy(); plot.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme]);

  useEffect(() => {
    const u = plot.current;
    if (!u || !data[0].length) return;
    const last = data[0][data[0].length - 1] as number;
    const lastLive = series.ts.length ? series.ts[series.ts.length - 1] / 1000 : last;
    const max = Math.max(last, lastLive) + 2;
    u.setData(data as uPlot.AlignedData, false);
    u.setScale("x", { min: lastLive - WINDOW_MS / 1000, max });
  }, [data, series.ts]);

  return (
    <section className="panel" aria-label="Success rate chart">
      <h2>
        <span style={{ whiteSpace: "nowrap" }}>Success rate</span>
        <span className="legend" style={{ textTransform: "none", letterSpacing: 0, fontFamily: "IBM Plex Sans", fontWeight: 400 }}>
          <span><i style={{ background: "var(--overall)" }} />Overall</span>
          <span><i style={{ background: "var(--bx)" }} />BANK_X</span>
          <span><i style={{ background: "var(--by)" }} />BANK_Y</span>
          <span><i style={{ background: "var(--bz)" }} />BANK_Z</span>
          <span><i style={{ background: "var(--twin)" }} />Predicted</span>
          <span><i style={{ background: "var(--warn)" }} />Deploy</span>
        </span>
      </h2>
      <div className="chart-wrap" ref={host} role="img" aria-label="Line chart of success rate by bank over the last two and a half minutes" />
    </section>
  );
}
