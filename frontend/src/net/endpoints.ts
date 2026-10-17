/** Public backend origin, baked into the Vercel build by Vite. Empty keeps local proxy routing. */
const backendOrigin = () => (import.meta.env.VITE_BACKEND_URL ?? "").trim().replace(/\/+$/, "");

export function backendUrl(path: string): string {
  // Preserve absolute bulletin URLs and in-browser mock image data.
  if (/^(?:https?:|data:|blob:)/i.test(path)) return path;
  const origin = backendOrigin();
  return origin ? new URL(path, origin + "/").href : path;
}

export function backendWebSocketUrl(): string {
  const url = new URL("/ws", backendOrigin() || window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}
