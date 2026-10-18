import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, clearToken, setToken } from "./api";

const stored = new Map<string, string>();
beforeEach(() => {
  vi.restoreAllMocks();
  stored.clear();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
  });
  vi.stubGlobal("window", new EventTarget());
  setToken("existing");
});

function response(status: number, body: unknown) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("authenticated requests", () => {
  it("uses a candidate token without persisting it before validation", async () => {
    const request = vi
      .fn()
      .mockResolvedValue(response(401, { detail: "invalid" }));
    vi.stubGlobal("fetch", request);
    await expect(
      api("/status", { token: "wrong", quiet401: true }),
    ).rejects.toThrow("invalid");
    expect(stored.get("aniopen-token")).toBe("existing");
    expect(request.mock.calls[0][1].headers.Authorization).toBe("Bearer wrong");
  });

  it("signals authentication loss and pauses polling without clearing saved credentials", async () => {
    const lost = vi.fn();
    window.addEventListener("aniopen-unauthorized", lost);
    const request = vi
      .fn()
      .mockResolvedValue(response(401, { detail: "expired" }));
    vi.stubGlobal("fetch", request);
    await expect(api("/config")).rejects.toThrow("expired");
    expect(lost).toHaveBeenCalledTimes(1);
    await expect(api("/status")).rejects.toThrow("未保存的配置会保留");
    expect(request).toHaveBeenCalledTimes(1);
    setToken("new-valid");
    request.mockResolvedValue(response(200, { ok: true }));
    expect(await api("/status")).toEqual({ ok: true });
  });

  it("handles an HTML 502 response without exposing the proxy document", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(502, "<html>upstream error</html>")),
    );
    await expect(api("/status")).rejects.toThrow("HTTP 502");
  });

  it("locks the session even if a 401 response is HTML", async () => {
    const lost = vi.fn();
    window.addEventListener("aniopen-unauthorized", lost);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(401, "<html>expired</html>")),
    );
    await expect(api("/status")).rejects.toThrow("HTTP 401");
    expect(lost).toHaveBeenCalledTimes(1);
  });

  it("aborts slow requests with a useful message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_path, options) =>
          new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      ),
    );
    await expect(api("/status", { timeoutMs: 10 })).rejects.toThrow("请求超时");
  });

  it("clears a session explicitly and allows a new token to unlock it", async () => {
    clearToken();
    expect(stored.has("aniopen-token")).toBe(false);
    setToken("replacement");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(200, { ok: true })),
    );
    expect(await api("/status")).toEqual({ ok: true });
  });
});
