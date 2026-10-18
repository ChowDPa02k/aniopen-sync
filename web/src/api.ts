export type Obj = Record<string, any>;
type Options = RequestInit & {
  token?: string;
  timeoutMs?: number;
  quiet401?: boolean;
};
let authenticationPaused = false;

export function setToken(token: string) {
  sessionStorage.setItem("aniopen-token", token);
  authenticationPaused = false;
}

export function clearToken() {
  sessionStorage.removeItem("aniopen-token");
  authenticationPaused = true;
}

export async function api(path: string, options: Options = {}) {
  const {
    token: override,
    timeoutMs = 15000,
    quiet401 = false,
    ...request
  } = options;
  if (authenticationPaused && override === undefined && path !== "/health")
    throw new Error("会话已失效，请重新验证令牌；未保存的配置会保留");
  const token = override ?? sessionStorage.getItem("aniopen-token") ?? "";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch("/api" + path, {
      ...request,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: "Bearer " + token } : {}),
        ...request.headers,
      },
    });
    if (response.status === 401 && !quiet401) {
      authenticationPaused = true;
      window.dispatchEvent(new Event("aniopen-unauthorized"));
    }
    const body = await response.text();
    let result: Obj;
    try {
      result = JSON.parse(body);
    } catch {
      throw new Error(
        `服务返回非 JSON 响应（HTTP ${response.status}），请检查反向代理或服务状态`,
      );
    }
    if (!response.ok) {
      const detail = result.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d: Obj) => `${d.loc?.join(".")}: ${d.msg}`).join("\n")
            : `请求失败（HTTP ${response.status}）`,
      );
    }
    return result;
  } catch (error) {
    if (controller.signal.aborted)
      throw new Error("请求超时，请检查网络或服务状态");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
