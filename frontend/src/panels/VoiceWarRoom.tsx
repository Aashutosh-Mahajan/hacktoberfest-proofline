import { useEffect, useRef, useState } from "react";
import { C, box, btn, h3, input } from "./ui";
import { api } from "../net/api";

export interface VoiceItem { query_id: string; transcript?: string; lang?: string; answer?: string; answer_lang?: string; evidence_ids?: string[]; }

/** items: build from voice.query / voice.answer events with mergeVoice(). */
export function VoiceWarRoom({ items, speak = true }: { items: VoiceItem[]; speak?: boolean }) {
  const [text, setText] = useState("");
  const [recording, setRecording] = useState(false);
  const [status, setStatus] = useState("");
  const recRef = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const spoken = useRef<Set<string>>(new Set());

  const awaiting = useRef(false); // only read aloud answers to questions asked from this tab (not replayed history)

  useEffect(() => {
    if (items.length && items.every((i) => i.answer)) setStatus((st) => (st === "Thinking..." ? "" : st));
    if (!awaiting.current) for (const it of items) if (it.answer) spoken.current.add(it.query_id); // history: never read aloud
    if (!speak || typeof speechSynthesis === "undefined" || !awaiting.current) return;
    for (const it of items) {
      if (it.answer && !spoken.current.has(it.query_id)) {
        awaiting.current = false;
        spoken.current.add(it.query_id);
        const u = new SpeechSynthesisUtterance(it.answer);
        u.lang = it.answer_lang === "hi" ? "hi-IN" : "en-IN";
        speechSynthesis.speak(u);
      }
    }
  }, [items, speak]);

  const stop = () => { const r = recRef.current; if (r && r.state === "recording") r.stop(); setRecording(false); };
  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunks.current = [];
      rec.ondataavailable = (e) => { if (e.data.size) chunks.current.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks.current, { type: rec.mimeType || "audio/webm" });
        setStatus("Sending...");
        const r = await api.voice(blob);
        const fb = r.fallback === "audio_unsupported";
        awaiting.current = !!r.ok && !fb;
        setStatus(fb ? String(r.hint ?? "Voice is not available here. Type your question instead.") : r.ok ? "Thinking..." : "Voice failed: " + (r.error ?? "error"));
      };
      recRef.current = rec; rec.start(); setRecording(true); setStatus("Listening...");
      setTimeout(() => { if (rec.state === "recording") stop(); }, 30000);
    } catch { setStatus("Microphone unavailable - use text input."); }
  };

  const ask = async () => {
    const t = text.trim(); if (!t) return;
    setText(""); setStatus("Thinking...");
    const r = await api.ask(t);
    awaiting.current = !!r.ok;
    setStatus(r.ok ? "Thinking..." : "Ask failed: " + (r.error ?? "error"));
  };

  return (
    <div style={box} data-testid="voice-war-room">
      <h3 style={h3}>Voice war-room</h3>
      <div style={{ maxHeight: 220, overflowY: "auto", marginBottom: 8 }}>
        {items.length === 0 && <div style={{ color: C.muted }}>Hold the mic and ask: &quot;Why is BANK_X failing?&quot; (EN / हिन्दी)</div>}
        {items.map((it) => (
          <div key={it.query_id} style={{ marginBottom: 8 }}>
            {it.transcript && <div style={{ color: C.accent }}>You: {it.transcript}</div>}
            {it.answer && <div>ProofLine: {it.answer}{it.evidence_ids?.length ? <span style={{ color: C.muted }}> [{it.evidence_ids.join(", ")}]</span> : null}</div>}
          </div>))}
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <button style={{ ...btn, background: recording ? C.warn : C.accent }} onMouseDown={start} onMouseUp={stop} onMouseLeave={() => recording && stop()}
          onTouchStart={start} onTouchEnd={stop} aria-label="Push to talk"
          onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat && !recording) { e.preventDefault(); void start(); } }}
          onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); stop(); } }}>{recording ? "Release to send" : "Hold to talk"}</button>
        <input style={input} aria-label="Ask a question" value={text} placeholder="...or type a question" onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()} />
        <button style={btn} onClick={ask}>Ask</button>
      </div>
      {status && <div style={{ color: C.muted, marginTop: 6, fontSize: 12 }}>{status}</div>}
    </div>
  );
}

/** Merge voice.query / voice.answer event data into VoiceItem[] (pure). */
export function mergeVoice(
  queries: { query_id: string; transcript: string; lang: string }[],
  answers: { query_id: string; text: string; lang: string; evidence_ids: string[] }[],
): VoiceItem[] {
  const map = new Map<string, VoiceItem>();
  for (const q of queries) map.set(q.query_id, { query_id: q.query_id, transcript: q.transcript, lang: q.lang });
  for (const a of answers) map.set(a.query_id, { ...(map.get(a.query_id) ?? { query_id: a.query_id }), answer: a.text, answer_lang: a.lang, evidence_ids: a.evidence_ids });
  return [...map.values()];
}
