import { useEffect, useRef, useState } from "react";
import { useStore } from "../store/store";
import type { AppState } from "../store/types";

/** Select a slice of the store but re-render at most once per `ms` (KPIs and tiles need 2 Hz, not 4 Hz). */
export function useThrottled<T>(select: (s: AppState) => T, ms = 500): T {
  const sel = useRef(select); sel.current = select;
  const [v, setV] = useState<T>(() => select(useStore.getState()));
  useEffect(() => {
    let last = 0, timer = 0, pending: { v: T } | null = null;
    const flush = () => { timer = 0; if (pending) { setV(pending.v); pending = null; last = Date.now(); } };
    setV(sel.current(useStore.getState())); // catch up with anything that landed before mount
    const unsub = useStore.subscribe((s) => {
      const nv = sel.current(s);
      pending = { v: nv };
      if (timer) return;
      const wait = Math.max(0, ms - (Date.now() - last));
      if (wait === 0) flush(); else timer = window.setTimeout(flush, wait);
    });
    return () => { unsub(); window.clearTimeout(timer); };
  }, [ms]);
  return v;
}
