// HTTP-клиент ядра. Адрес и токен приходят из Electron (или из адресной строки в браузере).
import { bridge } from "@/lib/bridge";

let base = "";
let token = "";
let readyResolve: () => void;
const ready = new Promise<void>((r) => (readyResolve = r));

export async function initCore(): Promise<{ url: string; token: string }> {
  for (let attempt = 0; ; attempt++) {
    const info = await bridge.core();
    if (info) {
      base = info.url.replace(/\/$/, "");
      token = info.token;
      readyResolve();
      return info;
    }
    await new Promise((r) => setTimeout(r, Math.min(2000, 200 + attempt * 200)));
  }
}

export function coreUrl(): string {
  return base;
}

export function coreToken(): string {
  return token;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  await ready;
  let res: Response;
  try {
    res = await fetch(base + path, {
      method,
      headers: {
        "X-Jarvis-Token": token,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError("Нет связи с ядром Jarvis", 0);
  }
  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      /* тело не JSON */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>("GET", path, undefined, signal),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T>(path: string, body: unknown = {}) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown = {}) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}
