import type { ReactNode } from "react";
import { C, btn } from "./ui";

function inline(s: string, k: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;
  let last = 0, i = 0, m: RegExpExecArray | null;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("**")) out.push(<strong key={k + i}>{t.slice(2, -2)}</strong>);
    else if (t.startsWith("`")) out.push(<code key={k + i} style={{ background: C.bg, padding: "0 4px", borderRadius: 3 }}>{t.slice(1, -1)}</code>);
    else out.push(<em key={k + i}>{t.slice(1, -1)}</em>);
    last = m.index + t.length; i++;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

/** Minimal safe markdown -> React elements (only text nodes, no HTML injection). */
export function renderMarkdown(md: string): ReactNode[] {
  const lines = md.replace(/\r/g, "").split("\n");
  const out: ReactNode[] = [];
  let list: string[] = [], para: string[] = [], code: string[] | null = null, n = 0;
  const flushList = () => { if (list.length) { const items = list; out.push(<ul key={"u" + n++}>{items.map((x, i) => <li key={i}>{inline(x, "l" + i)}</li>)}</ul>); list = []; } };
  const flushPara = () => { if (para.length) { out.push(<p key={"p" + n++}>{inline(para.join(" "), "p")}</p>); para = []; } };
  const isRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
  for (let idx = 0; idx < lines.length; idx++) {
    const l = lines[idx];
    if (code) { if (l.startsWith("```")) { out.push(<pre key={"c" + n++} style={{ background: C.bg, padding: 8, borderRadius: 6, overflowX: "auto" }}>{code.join("\n")}</pre>); code = null; } else code.push(l); continue; }
    if (l.startsWith("```")) { flushList(); flushPara(); code = []; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(l);
    if (h) { flushList(); flushPara(); const Tag = ("h" + Math.min(5, h[1].length + 1)) as "h2" | "h3" | "h4" | "h5"; out.push(<Tag key={"h" + n++} style={{ margin: "12px 0 4px" }}>{inline(h[2], "h")}</Tag>); continue; }
    const li = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(l);
    if (li) { flushPara(); list.push(li[1]); continue; }
    if (isRow(l)) {
      flushList(); flushPara();
      const rows: string[][] = [];
      while (idx < lines.length && isRow(lines[idx])) {
        if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[idx])) rows.push(lines[idx].trim().slice(1, -1).split("|").map((c) => c.trim()));
        idx++;
      }
      idx--;
      out.push(<table key={"t" + n++} style={{ borderCollapse: "collapse", margin: "6px 0" }}><tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => { const Cell = i === 0 ? "th" : "td"; return <Cell key={j} style={{ border: `1px solid ${C.border}`, padding: "3px 8px", textAlign: "left" }}>{inline(c, "t")}</Cell>; })}</tr>)}</tbody></table>);
      continue;
    }
    if (!l.trim()) { flushList(); flushPara(); continue; }
    flushList(); para.push(l.trim());
  }
  if (code) out.push(<pre key={"c" + n++}>{(code as string[]).join("\n")}</pre>);
  flushList(); flushPara();
  return out;
}

export function PostmortemModal({ markdown, open = true, onClose }: { markdown: string; open?: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div role="dialog" aria-modal="true" aria-label="Postmortem" onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}>
      <div onClick={(e) => e.stopPropagation()} data-testid="postmortem-modal"
        style={{ background: C.panel, color: C.fg, border: `1px solid ${C.border}`, borderRadius: 10, width: "min(760px, 92vw)", maxHeight: "86vh", overflow: "auto", padding: 20, fontSize: 14, lineHeight: 1.5 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <b>Postmortem</b>
          <span style={{ display: "flex", gap: 6 }}>
            <button style={btn} onClick={() => { void navigator.clipboard?.writeText(markdown); }}>Copy</button>
            <button style={btn} onClick={onClose}>Close</button>
          </span>
        </div>
        <div>{renderMarkdown(markdown)}</div>
      </div>
    </div>
  );
}
