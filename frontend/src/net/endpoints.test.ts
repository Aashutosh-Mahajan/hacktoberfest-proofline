import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backendUrl, backendWebSocketUrl } from "./endpoints";
import { defaultWsUrl } from "./wsClient";
import { api } from "./api";
import { jsonCall } from "../panels/ui";

beforeEach(() => vi.stubEnv("VITE_BACKEND_URL", ""));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); });

describe("separate frontend and backend hosts", () => {
  it("preserves local proxy routing when no remote backend is configured", () => {
    expect(backendUrl("/api/health")).toBe("/api/health");
    const expected = new URL("/ws", window.location.origin);
    expected.protocol = expected.protocol === "https:" ? "wss:" : "ws:";
    expect(defaultWsUrl()).toBe(expected.href);
  });

  it("sends API calls and secure WebSockets to the Render origin", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "https://proofline-example.onrender.com/");
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response('{"ok":true}'));
    await api.health();
    expect(fetcher).toHaveBeenCalledWith("https://proofline-example.onrender.com/api/health", expect.objectContaining({ method: "GET" }));
    expect(backendWebSocketUrl()).toBe("wss://proofline-example.onrender.com/ws");
    expect(defaultWsUrl()).toBe(backendWebSocketUrl());
  });

  it("uses the same remote origin for voice uploads and panel actions", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "https://proofline-example.onrender.com");
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response('{"ok":true}'));
    await api.voice(new Blob(["test"], { type: "audio/webm" }));
    expect(fetcher).toHaveBeenCalledWith("https://proofline-example.onrender.com/api/voice", expect.objectContaining({ method: "POST", body: expect.any(FormData) }));
    await jsonCall("POST", "/api/ask", { text: "What happened?" });
    expect(fetcher).toHaveBeenCalledWith("https://proofline-example.onrender.com/api/ask", expect.objectContaining({ method: "POST" }));
  });

  it("loads backend bulletin images while preserving mock data images", () => {
    vi.stubEnv("VITE_BACKEND_URL", "https://proofline-example.onrender.com");
    expect(backendUrl("/api/bulletin/BANK_X.png")).toBe("https://proofline-example.onrender.com/api/bulletin/BANK_X.png");
    expect(backendUrl("data:image/svg+xml,test")).toBe("data:image/svg+xml,test");
    expect(backendUrl("https://images.example.com/bulletin.png")).toBe("https://images.example.com/bulletin.png");
  });
});
