import type { AimPointerMode, AimStateSnapshot } from "../types/activity";
import {
  appendNoStoreNonce,
  buildQueryStringFromPayload,
  fetchWithTimeout,
  resolveApiCandidates,
  resolveStrictApiCandidates,
} from "./httpClient";
import type { HttpTransportMeta, HttpTransportResult } from "./lobbyApi";

export type AimSyncInput = {
  roomId: string;
  userId: string;
  visible: boolean;
  angle: number;
  cueX?: number | null;
  cueY?: number | null;
  power?: number | null;
  seq?: number;
  mode: AimPointerMode;
};

type AimEnvelope = {
  aim?: unknown;
  error?: string;
  detail?: string;
};

function parseJsonSafely<T>(raw: string): T | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function buildResponsePreview(raw: string, maxLength = 320): string {
  const compact = raw.replace(/\s+/g, " ").trim();
  if (compact.length <= maxLength) return compact;
  return `${compact.slice(0, Math.max(0, maxLength - 1))}…`;
}

function buildTransportMeta(label: string, url: string, response: Response, raw: string): HttpTransportMeta {
  return {
    label,
    url,
    status: response.status,
    contentType: response.headers.get("content-type"),
    responsePreview: buildResponsePreview(raw),
  };
}

function normalizeBoolean(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (normalized === "true" || normalized === "1") return true;
    if (normalized === "false" || normalized === "0" || normalized === "") return false;
  }
  return null;
}

function normalizeNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function normalizeString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function normalizeAimState(raw: unknown): AimStateSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const roomId = normalizeString(value.roomId);
  const userId = normalizeString(value.userId);
  const visible = normalizeBoolean(value.visible);
  const angle = normalizeNumber(value.angle);
  const cueX = value.cueX === null || value.cueX === undefined || value.cueX === "" ? null : normalizeNumber(value.cueX);
  const cueY = value.cueY === null || value.cueY === undefined || value.cueY === "" ? null : normalizeNumber(value.cueY);
  const power = normalizeNumber(value.power) ?? 0;
  const seq = normalizeNumber(value.seq) ?? 0;
  const updatedAt = normalizeNumber(value.updatedAt) ?? Date.now();
  const snapshotRevision = normalizeNumber(value.snapshotRevision) ?? 0;
  const rawMode = normalizeString(value.mode);
  const mode: AimPointerMode = rawMode === "aim" || rawMode === "place" || rawMode === "power" || rawMode === "idle"
    ? rawMode
    : "idle";

  if (!roomId || !userId || visible === null || angle === null) return null;

  return {
    roomId,
    userId,
    visible,
    angle,
    cueX,
    cueY,
    power,
    seq,
    mode,
    updatedAt,
    snapshotRevision,
  };
}

function dedupeVariants<T extends { label: string; url: string; init: RequestInit }>(variants: T[]): T[] {
  const seen = new Set<string>();
  const deduped: T[] = [];
  for (const variant of variants) {
    const method = (variant.init.method ?? "GET").toUpperCase();
    const body = typeof variant.init.body === "string" ? variant.init.body : "";
    const key = `${method}|${variant.url}|${body}`;
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(variant);
  }
  return deduped;
}

export async function syncAimStateRequest(payload: AimSyncInput): Promise<HttpTransportResult<{ aim?: AimStateSnapshot | null; error?: string; detail?: string }>> {
  const attempts: string[] = [];
  const bodyJson = JSON.stringify(payload);
  const bodyForm = buildQueryStringFromPayload(payload);
  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

  for (const baseUrl of resolveStrictApiCandidates("/games/aim")) {
    requestVariants.push({
      label: `API_POST_JSON:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: bodyJson,
        keepalive: true,
      },
    });
    requestVariants.push({
      label: `API_POST_FORM:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        credentials: "same-origin",
        body: bodyForm,
        keepalive: true,
      },
    });
  }

  for (const baseUrl of resolveApiCandidates("/balance")) {
    requestVariants.push({
      label: `BALANCE_POST_FORM:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        credentials: "same-origin",
        body: buildQueryStringFromPayload({ action: "game_aim_sync", ...payload }),
        keepalive: true,
      },
    });
    const queryUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    const params = new URLSearchParams(buildQueryStringFromPayload({ action: "game_aim_sync", ...payload }));
    params.forEach((value, key) => queryUrl.searchParams.set(key, value));
    requestVariants.push({
      label: `BALANCE_GET_QUERY:${baseUrl}`,
      url: queryUrl.toString(),
      init: { method: "GET", credentials: "same-origin", cache: "no-store", keepalive: true },
    });
  }

  for (const variant of dedupeVariants(requestVariants)) {
    try {
      const response = await fetchWithTimeout(variant.url, variant.init, 1200);
      const raw = await response.text();
      const contentType = response.headers.get("content-type") ?? "";
      const trimmed = raw.trim();
      if (trimmed.startsWith("<") || /text\/html/i.test(contentType)) {
        attempts.push(`${variant.label}:${response.status}:html_response`);
        continue;
      }
      const parsed = parseJsonSafely<AimEnvelope>(raw);
      const normalizedAim = normalizeAimState(parsed?.aim);
      if (response.ok) {
        return {
          data: { aim: normalizedAim, error: parsed?.error, detail: parsed?.detail },
          attempts,
          okLabel: variant.label,
          okMeta: buildTransportMeta(variant.label, variant.url, response, raw),
          errorCode: null,
          errorDetail: null,
          errorStatus: null,
          errorPayload: null,
        };
      }
      attempts.push(`${variant.label}:${response.status}:${parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty")}`);
    } catch (error) {
      attempts.push(`${variant.label}:exception:${error instanceof Error ? error.message : "unknown"}`);
    }
  }

  return { data: null, attempts, okLabel: null, errorCode: null, errorDetail: null, errorStatus: null, errorPayload: null };
}

export async function fetchAimStateRequest(roomId: string): Promise<HttpTransportResult<{ aim?: AimStateSnapshot | null; error?: string; detail?: string }>> {
  const attempts: string[] = [];
  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

  for (const baseUrl of resolveStrictApiCandidates(`/games/${encodeURIComponent(roomId)}/aim`)) {
    const url = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    requestVariants.push({
      label: `API_GET:${baseUrl}`,
      url: url.toString(),
      init: { method: "GET", credentials: "same-origin", cache: "no-store" },
    });
  }

  for (const baseUrl of resolveApiCandidates("/balance")) {
    const url = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    url.searchParams.set("action", "game_aim_get");
    url.searchParams.set("roomId", roomId);
    requestVariants.push({
      label: `BALANCE_GET_QUERY:${baseUrl}`,
      url: url.toString(),
      init: { method: "GET", credentials: "same-origin", cache: "no-store" },
    });
  }

  for (const variant of dedupeVariants(requestVariants)) {
    try {
      const response = await fetchWithTimeout(variant.url, variant.init, 1200);
      const raw = await response.text();
      const contentType = response.headers.get("content-type") ?? "";
      const trimmed = raw.trim();
      if (trimmed.startsWith("<") || /text\/html/i.test(contentType)) {
        attempts.push(`${variant.label}:${response.status}:html_response`);
        continue;
      }
      const parsed = parseJsonSafely<AimEnvelope>(raw);
      const normalizedAim = normalizeAimState(parsed?.aim);
      if (response.ok) {
        return {
          data: { aim: normalizedAim, error: parsed?.error, detail: parsed?.detail },
          attempts,
          okLabel: variant.label,
          okMeta: buildTransportMeta(variant.label, variant.url, response, raw),
          errorCode: null,
          errorDetail: null,
          errorStatus: null,
          errorPayload: null,
        };
      }
      attempts.push(`${variant.label}:${response.status}:${parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty")}`);
    } catch (error) {
      attempts.push(`${variant.label}:exception:${error instanceof Error ? error.message : "unknown"}`);
    }
  }

  return { data: null, attempts, okLabel: null, errorCode: null, errorDetail: null, errorStatus: null, errorPayload: null };
}
