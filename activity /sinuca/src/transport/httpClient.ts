export const DEFAULT_PUBLIC_HOST = (import.meta.env.VITE_ACTIVITY_DASHBOARD_PUBLIC_HOST as string | undefined)?.trim() || (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || "osakaagiota.duckdns.org";

export class DashboardHttpError extends Error {
  status: number;
  url: string;
  code: string;
  raw: string | null;

  constructor(message: string, status: number, url: string, code = "request_failed", raw: string | null = null) {
    super(message);
    this.name = "DashboardHttpError";
    this.status = status;
    this.url = url;
    this.code = code;
    this.raw = raw;
  }
}

export function joinBaseAndPath(base: string, path: string) {
  const normalizedBase = base.endsWith("/") ? base.slice(0, -1) : base;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (normalizedBase.endsWith("/api") && normalizedPath === "/api") return normalizedBase;
  if (normalizedBase.endsWith("/api") && normalizedPath.startsWith("/api/")) {
    return `${normalizedBase}${normalizedPath.slice(4)}`;
  }
  return `${normalizedBase}${normalizedPath}`;
}

function normalizeBase(base: string) {
  const trimmed = base.trim();
  if (!trimmed) return "";
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

export function resolvePublicBaseCandidates() {
  const configuredApiBase = normalizeBase((import.meta.env.VITE_ACTIVITY_DASHBOARD_API_BASE_URL as string | undefined)?.trim() || (import.meta.env.VITE_SINUCA_API_BASE_URL as string | undefined)?.trim() || "");
  const configuredPublicHost = normalizeBase((import.meta.env.VITE_ACTIVITY_DASHBOARD_PUBLIC_HOST as string | undefined)?.trim() || (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || "");
  const directHost = normalizeBase(configuredPublicHost || DEFAULT_PUBLIC_HOST);
  const currentOrigin = typeof window !== "undefined" ? window.location.origin : "";

  return [configuredApiBase, directHost, currentOrigin]
    .filter(Boolean)
    .filter((value, index, array) => array.indexOf(value) === index);
}

export function resolveApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiPath = normalizedPath.startsWith("/api/") || normalizedPath === "/api"
    ? normalizedPath
    : `/api${normalizedPath}`;
  const legacyPath = normalizedPath.replace(/^\/api(?=\/|$)/, "") || "/";
  const candidates: string[] = [];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, apiPath));
  }
  candidates.push(apiPath);

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, legacyPath));
  }
  candidates.push(legacyPath);

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function resolveStrictApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiPath = normalizedPath.startsWith("/api/") || normalizedPath === "/api"
    ? normalizedPath
    : `/api${normalizedPath}`;
  const candidates: string[] = [];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, apiPath));
  }
  candidates.push(apiPath);

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function resolveTokenCandidates() {
  const candidates: string[] = [];
  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, "/token"));
  }
  candidates.push("/token");

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, "/api/token"));
  }
  candidates.push("/api/token");

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit, timeoutMs = 2500) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
}

function looksLikeHtml(raw: string, contentType: string) {
  const text = raw.trim().slice(0, 80).toLowerCase();
  return contentType.includes("text/html") || text.startsWith("<!doctype html") || text.startsWith("<html");
}

function payloadMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  for (const key of ["error", "detail", "message", "reason"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function sanitizeRawForMessage(raw: string, contentType: string) {
  if (looksLikeHtml(raw, contentType)) {
    return "A rota da API devolveu HTML do frontend. O proxy/fallback ainda não encaminhou essa chamada para o backend.";
  }
  const text = raw.replace(/\s+/g, " ").trim();
  return text.slice(0, 220) || "Resposta vazia";
}

export async function fetchJsonFromCandidate<T>(url: string, init: RequestInit, timeoutMs = 5000): Promise<T> {
  const response = await fetchWithTimeout(url, {
    ...init,
    cache: "no-store",
    credentials: init.credentials ?? "same-origin",
    headers: {
      Accept: "application/json",
      ...(init.headers ?? {}),
    },
  }, timeoutMs);
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  const raw = await response.text();

  if (looksLikeHtml(raw, contentType)) {
    throw new DashboardHttpError(
      "A chamada caiu no HTML do frontend, não na API do dashboard.",
      response.status || 0,
      url,
      "html_instead_of_json",
      raw,
    );
  }

  let parsed: unknown = null;
  try {
    parsed = raw ? JSON.parse(raw) : null;
  } catch {
    throw new DashboardHttpError(
      sanitizeRawForMessage(raw, contentType),
      response.status || 0,
      url,
      "invalid_json",
      raw,
    );
  }

  if (!response.ok) {
    const message = payloadMessage(parsed) ?? `Falha HTTP ${response.status}`;
    throw new DashboardHttpError(message, response.status, url, "http_error", raw);
  }

  return parsed as T;
}

export async function fetchJsonFromCandidates<T>(candidates: string[], init: RequestInit, timeoutMs = 5000): Promise<T> {
  const attempts: string[] = [];
  let lastError: unknown = null;

  for (const url of candidates) {
    try {
      return await fetchJsonFromCandidate<T>(url, init, timeoutMs);
    } catch (error) {
      lastError = error;
      if (error instanceof DashboardHttpError) {
        attempts.push(`${error.status || "sem_status"} ${error.code} ${url}`);
      } else {
        const text = error instanceof Error ? error.message : String(error);
        attempts.push(`erro ${url}: ${text}`);
      }
    }
  }

  if (lastError instanceof DashboardHttpError) {
    const detail = attempts.slice(0, 6).join(" · ");
    throw new DashboardHttpError(
      `${lastError.message}${detail ? ` (${detail})` : ""}`,
      lastError.status,
      lastError.url,
      lastError.code,
      lastError.raw,
    );
  }

  const message = lastError instanceof Error ? lastError.message : String(lastError || "api_unreachable");
  throw new Error(`${message}${attempts.length ? ` (${attempts.slice(0, 6).join(" · ")})` : ""}`);
}
