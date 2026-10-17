import { memo, useEffect, useRef } from "react";
import type uPlot from "uplot";
import { useStore } from "../store/store";
import { chartBuffer, buildChartData } from "../store/chartBuffer";
import { fmtClock } from "../lib/format";
import { useTheme } from "../lib/theme";

export { buildChartData };

const WINDOW_S = 150;
const FORECAST_S = 45;

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/**
 * The hero chart. History lives in `chartBuffer` (outside React); uPlot is created once per theme and fed with
 * setData at most once per animation frame, so a 4 Hz tick stream costs one canvas repaint and zero React renders.
 */
function HealthChartBase() {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const meta = useRef({ incStart: 0, incEnd: undefined as number | undefined, deploys: [] as { ts: number; n: number }[] });
  const previewRef = useRef<ReturnType<typeof useStore.getState>["preview"]>(undefined);
  const raf = useRef(0);
  const preview = useStore((s) => s.preview);
  const incStart = useStore((s) => s.incident?.start_ts ?? 0);
  const incEnd = useStore((s) => s.incident?.end_ts);
  const deploys = useStore((s) => s.deploys);
  const theme = useTheme((s) => s.theme);

  previewRef.current = preview;
  meta.current.incStart = incStart;
  meta.current.incEnd = incEnd;
  meta.current.deploys = deploys.map((d) => ({ ts: d.ts, n: d.number }));

  const paint = useRef(() => {});
  paint.current = () => {
    raf.current = 0;
    const u = plot.current;
    const b = chartBuffer.data;
    if (!u || !b.x.length) return;
    const pv = previewRef.current;
    const data = buildChartData(b, pv, chartBuffer.timeOffsetMs);
    const lastLive = b.x[b.x.length - 1];
    const lastData = data[0][data[0].length - 1] as number;
    const max = Math.max(lastLive + 2, Math.min(lastData, lastLive + FORECAST_S));
    u.setData(data as uPlot.AlignedData, false);
    u.setScale("x", { min: max - WINDOW_S, max });
  };
  const schedule = () => { if (!raf.current) raf.current = requestAnimationFrame(() => paint.current()); };

  useEffect(() => {
    let dead = false;
    let ro: ResizeObserver | undefined;
    let off: (() => void) | undefined;
    const el = host.current!;
    void import("uplot").then(({ default: UPlot }) => {
      if (dead) return;
      const lw = parseFloat(css("--lw")) || 1.5;
      const mk = (label: string, color: string, width = lw, dash?: number[]): uPlot.Series => ({
        label, stroke: color, width, dash, spanGaps: true, points: { show: false },
      });
      const opts: uPlot.Options = {
        width: el.clientWidth, height: el.clientHeight, padding: [8, 8, 0, 0],
        cursor: { show: false }, legend: { show: false },
        scales: { x: { time: false }, y: { range: (_u, min, max) => [Math.max(0, Math.min(min, 90) - 4), Math.max(100, max + 0.5)] } },
        axes: [
          { space: 90, stroke: css("--dim"), grid: { stroke: css("--line"), width: 1 }, ticks: { show: false }, values: (_u, v) => v.map((x) => fmtClock(x * 1000)), font: "12px JetBrains Mono" },
          { stroke: css("--dim"), grid: { stroke: css("--line"), width: 1 }, ticks: { show: false }, size: 48, values: (_u, v) => v.map((x) => x + "%"), font: "12px JetBrains Mono" },
        ],
        series: [
          {},
          mk("All payments", css("--overall"), lw * 1.8), mk("BANK_X", css("--bx")), mk("BANK_Y", css("--by")), mk("BANK_Z", css("--bz")),
          mk("Test of the fix (simulated)", css("--twin"), lw * 1.8, [6, 4]), mk("If we do nothing", css("--alert"), lw, [2, 4]),
        ],
        hooks: {
          drawClear: [(u) => {
            const { ctx } = u; const { left, top, width, height } = u.bbox;
            const m = meta.current;
            if (m.incStart) {
              const x0 = u.valToPos(chartBuffer.chartTime(m.incStart), "x", true);
              const x1 = m.incEnd ? u.valToPos(chartBuffer.chartTime(m.incEnd), "x", true) : left + width;
              ctx.fillStyle = css("--shade");
              ctx.fillRect(Math.max(left, x0), top, Math.max(0, Math.min(left + width, x1) - Math.max(left, x0)), height);
            }
          }],
          draw: [(u) => {
            const { ctx } = u; const { left, top, width, height } = u.bbox;
            ctx.save(); ctx.font = "12px JetBrains Mono"; ctx.strokeStyle = css("--warn"); ctx.fillStyle = css("--warn"); ctx.setLineDash([4, 4]);
            for (const d of meta.current.deploys) {
              const x = u.valToPos(chartBuffer.chartTime(d.ts), "x", true);
              if (x < left || x > left + width) continue;
              ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
              ctx.fillText("Update #" + d.n, x + 4, top + 14);
            }
            ctx.restore();
          }],
        },
      };
      const b = chartBuffer.data;
      const u = new UPlot(opts, buildChartData(b, previewRef.current, chartBuffer.timeOffsetMs) as uPlot.AlignedData, el);
      plot.current = u;
      ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: el.clientHeight }));
      ro.observe(el);
      off = chartBuffer.subscribe(schedule);
      schedule();
    });
    return () => { dead = true; off?.(); ro?.disconnect(); plot.current?.destroy(); plot.current = null; cancelAnimationFrame(raf.current); raf.current = 0; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme]);

  // preview / incident / deploy overlays change rarely: just repaint once
  useEffect(() => { schedule(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [preview, incStart, incEnd, deploys]);

  return <div className="chart-wrap" ref={host} role="img" aria-label="Line chart of the share of successful payments over the last two and a half minutes, per bank, with the simulated fix forecast" />;
}

export const HealthChart = memo(HealthChartBase);
