export class DashboardHttpError extends Error {
  status: number;
  code: string;
  payload: unknown;

  constructor(message: string, status = 0, code = "request_failed", payload: unknown = null) {
    super(message);
    this.name = "DashboardHttpError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

function payloadMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  for (const key of ["detail", "message", "error", "reason"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

export async function fetchDashboardJson<T>(path: string, init: RequestInit = {}, timeoutMs = 12000): Promise<T> {
  const normalizedPath = path.startsWith("/api/") || path === "/api" ? path : `/api${path.startsWith("/") ? path : `/${path}`}`;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(normalizedPath, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers || {}),
      },
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const raw = await response.text();
    let payload: unknown = null;
    if (raw) {
      if (!contentType.includes("application/json")) {
        throw new DashboardHttpError(
          response.ok ? "A API respondeu em um formato inesperado." : `Falha HTTP ${response.status}.`,
          response.status,
          "invalid_response_type",
          raw.slice(0, 300),
        );
      }
      try {
        payload = JSON.parse(raw);
      } catch {
        throw new DashboardHttpError("A API devolveu JSON inválido.", response.status, "invalid_json", raw.slice(0, 300));
      }
    }
    if (!response.ok) {
      throw new DashboardHttpError(payloadMessage(payload) || `Falha HTTP ${response.status}.`, response.status, "http_error", payload);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof DashboardHttpError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new DashboardHttpError("A solicitação demorou além do esperado.", 0, "timeout");
    }
    throw new DashboardHttpError(error instanceof Error ? error.message : "Falha de conexão.", 0, "network_error");
  } finally {
    window.clearTimeout(timeout);
  }
}
