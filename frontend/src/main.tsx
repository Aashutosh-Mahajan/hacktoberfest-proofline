import React from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { App } from "./App";
import { useStore } from "./store/store";
import { WsClient } from "./net/wsClient";
import { setTransport } from "./net/api";
import { MockServer } from "./dev/mockServer";

const params = new URLSearchParams(location.search);
try { const th = localStorage.getItem("mayday.theme"); if (th) document.documentElement.dataset.theme = th; } catch { /* storage unavailable */ }

export let ws: WsClient | null = null;
if (params.get("mock") === "1") {
  const mock = new MockServer((env) => useStore.getState().dispatch(env), { speed: Number(params.get("speed") ?? 1) });
  setTransport(mock.transport());
  useStore.getState().setConn("mock");
  mock.start();
} else {
  ws = new WsClient();
  ws.start();
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App onReconnect={() => ws?.forceReconnect()} /></React.StrictMode>);
