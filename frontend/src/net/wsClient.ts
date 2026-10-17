import type { Envelope } from "@contract";
import { useStore } from "../store/store";

export function defaultWsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return proto + "://" + location.host + "/ws";
}

/** WebSocket client: resume by last seq, exponential backoff, batched dispatch (one store write per frame). */
export class WsClient {
  private ws?: WebSocket;
  private timer?: number;
  private attempts = 0;
  private closed = false;
  private queue: Envelope[] = [];
  private raf = 0;
  private fallback = 0;
  private lastMsgAt = 0;
  private watchdog?: number;

  constructor(private urlBase = defaultWsUrl()) {}

  start() {
    this.closed = false;
    this.connect();
    this.watchdog = window.setInterval(() => {
      // 4 Hz metrics should never go quiet for long while live; reconnect on a stalled socket.
      if (this.ws?.readyState === WebSocket.OPEN && Date.now() - this.lastMsgAt > 15000) this.ws.close();
    }, 5000);
  }

  stop() {
    this.closed = true;
    window.clearTimeout(this.timer);
    window.clearInterval(this.watchdog);
    cancelAnimationFrame(this.raf);
    this.ws?.close();
  }

  /** For the hidden demo panel. */
  forceReconnect() { this.ws?.close(); }

  private connect() {
    // Always replay the whole bus ring (since=0): every snapshot fully resets the store and the agent timeline
    // (thoughts, tool calls) is rebuilt from the replayed events that follow it.
    const since = 0;
    useStore.getState().beginResync();
    useStore.getState().setConn(this.attempts === 0 && useStore.getState().snapSeq === 0 ? "connecting" : "reconnecting");
    let ws: WebSocket;
    try { ws = new WebSocket(this.urlBase + "?since=" + since); } catch { this.schedule(); return; }
    this.ws = ws;
    ws.onopen = () => { this.attempts = 0; this.lastMsgAt = Date.now(); useStore.getState().setConn("live"); };
    ws.onmessage = (ev) => {
      this.lastMsgAt = Date.now();
      try {
        const env = JSON.parse(ev.data as string) as Envelope;
        this.queue.push(env);
        if (!this.raf) { // one store write per frame; the timer covers hidden tabs where rAF is paused
          this.raf = requestAnimationFrame(() => this.flush());
          this.fallback = window.setTimeout(() => this.flush(), 250);
        }
      } catch { /* ignore malformed frame */ }
    };
    ws.onclose = () => { if (!this.closed) { useStore.getState().setConn("reconnecting"); this.schedule(); } };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }

  private flush() {
    cancelAnimationFrame(this.raf); window.clearTimeout(this.fallback);
    this.raf = 0;
    const batch = this.queue;
    this.queue = [];
    if (batch.length) useStore.getState().dispatchBatch(batch);
  }

  private schedule() {
    this.attempts++;
    const delay = Math.min(8000, 400 * 2 ** Math.min(this.attempts, 5));
    if (this.attempts > 6) useStore.getState().setConn("offline");
    this.timer = window.setTimeout(() => { if (!this.closed) this.connect(); }, delay);
  }
}
