import type { ActivityMode, RoomSnapshot } from "../types/activity";
import {
  appendNoStoreNonce,
  buildQueryStringFromPayload,
  fetchWithTimeout,
  resolveApiCandidates,
  resolveLegacyBalanceAction,
  resolveStrictApiCandidates,
} from "./httpClient";

export interface HttpTransportMeta {
  label: string;
  url: string;
  status: number | null;
  contentType: string | null;
  responsePreview: string | null;
}

export interface HttpTransportResult<T> {
  data: T | null;
  attempts: string[];
  okLabel: string | null;
  okMeta?: HttpTransportMeta | null;
  errorCode?: string | null;
  errorDetail?: string | null;
  errorStatus?: number | null;
  errorPayload?: Record<string, unknown> | null;
}

export async function fetchRoomsRequest(params: {
  mode: ActivityMode;
  guildId?: string | null;
  channelId?: string | null;
}): Promise<HttpTransportResult<{ rooms?: RoomSnapshot[]; error?: string }>> {
  const attempts: string[] = [];

  for (const baseUrl of resolveStrictApiCandidates("/rooms")) {
    try {
      const url = appendNoStoreNonce(baseUrl, `${Date.now()}`);
      url.searchParams.set("mode", params.mode);
      if (params.guildId) url.searchParams.set("guildId", params.guildId);
      if (params.channelId) url.searchParams.set("channelId", params.channelId);
      const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
      const raw = await response.text();
      const parsed = raw ? JSON.parse(raw) as { rooms?: RoomSnapshot[]; error?: string } : null;
      if (response.ok && Array.isArray(parsed?.rooms)) {
        return { data: parsed, attempts, okLabel: `api:${baseUrl}` };
      }
      attempts.push(`API:${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`API:${baseUrl}:exception:${message}`);
    }
  }

  const legacyAction = resolveLegacyBalanceAction("/rooms");
  if (legacyAction) {
    for (const baseUrl of resolveApiCandidates("/balance")) {
      try {
        const url = appendNoStoreNonce(baseUrl, `${Date.now()}`);
        url.searchParams.set("action", legacyAction);
        url.searchParams.set("mode", params.mode);
        if (params.guildId) url.searchParams.set("guildId", params.guildId);
        if (params.channelId) url.searchParams.set("channelId", params.channelId);
        const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { rooms?: RoomSnapshot[]; error?: string } : null;
        if (response.ok && Array.isArray(parsed?.rooms)) {
          return { data: parsed, attempts, okLabel: `balance:${baseUrl}` };
        }
        attempts.push(`BALANCE:${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`BALANCE:${baseUrl}:exception:${message}`);
      }
    }
  }

  return { data: null, attempts, okLabel: null, errorCode: null, errorDetail: null, errorStatus: null, errorPayload: null };
}

export async function fetchRoomStateRequest(roomId: string): Promise<HttpTransportResult<{ room?: RoomSnapshot | null; error?: string }>> {
  const attempts: string[] = [];

  for (const baseUrl of resolveStrictApiCandidates(`/rooms/${encodeURIComponent(roomId)}`)) {
    try {
      const requestUrl = appendNoStoreNonce(baseUrl, `${Date.now()}`);
      const response = await fetchWithTimeout(requestUrl.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
      const raw = await response.text();
      const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; error?: string } : null;
      if (response.ok) {
        return { data: parsed, attempts, okLabel: `api:${baseUrl}` };
      }
      attempts.push(`API:${requestUrl.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`API:${baseUrl}:exception:${message}`);
    }
  }

  const legacyAction = resolveLegacyBalanceAction(`/rooms/${roomId}`);
  if (legacyAction) {
    for (const baseUrl of resolveApiCandidates("/balance")) {
      try {
        const requestUrl = appendNoStoreNonce(baseUrl, `${Date.now()}`);
        requestUrl.searchParams.set("action", legacyAction);
        requestUrl.searchParams.set("roomId", roomId);
        const response = await fetchWithTimeout(requestUrl.toString(), { method: "GET", credentials: "same-origin", cache: "no-store" });
        const raw = await response.text();
        const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; error?: string } : null;
        if (response.ok) {
          return { data: parsed, attempts, okLabel: `balance:${baseUrl}` };
        }
        attempts.push(`BALANCE:${requestUrl.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        attempts.push(`BALANCE:${baseUrl}:exception:${message}`);
      }
    }
  }

  return { data: null, attempts, okLabel: null, errorCode: null, errorDetail: null, errorStatus: null, errorPayload: null };
}

export async function postRoomActionRequest(path: string, payload: Record<string, unknown>): Promise<HttpTransportResult<{ room?: RoomSnapshot | null; error?: string; detail?: string }>> {
  const attempts: string[] = [];
  let errorCode: string | null = null;
  let errorDetail: string | null = null;
  let errorStatus: number | null = null;
  let errorPayload: Record<string, unknown> | null = null;
  const query = buildQueryStringFromPayload(payload);
  const legacyAction = resolveLegacyBalanceAction(path);

  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];
  for (const baseUrl of resolveStrictApiCandidates(path)) {
    requestVariants.push({
      label: `API_POST_JSON:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      },
    });
    requestVariants.push({
      label: `API_POST_FORM:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        credentials: "same-origin",
        body: query,
      },
    });
    const getUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    if (query) {
      const queryUrl = new URL(getUrl.toString(), window.location.origin);
      const params = new URLSearchParams(query);
      params.forEach((value, key) => queryUrl.searchParams.set(key, value));
      requestVariants.push({
        label: `API_GET_QUERY:${baseUrl}`,
        url: queryUrl.toString(),
        init: {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
        },
      });
    }
  }

  if (legacyAction) {
    for (const baseUrl of resolveApiCandidates("/balance")) {
      const queryUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
      queryUrl.searchParams.set("action", legacyAction);
      const params = new URLSearchParams(query);
      params.forEach((value, key) => queryUrl.searchParams.set(key, value));
      requestVariants.push({
        label: `BALANCE_GET_QUERY:${baseUrl}`,
        url: queryUrl.toString(),
        init: {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
        },
      });
      requestVariants.push({
        label: `BALANCE_POST_FORM:${baseUrl}`,
        url: baseUrl,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          credentials: "same-origin",
          body: buildQueryStringFromPayload({ action: legacyAction, ...payload }),
        },
      });
    }
  }

  for (const variant of requestVariants) {
    try {
      const response = await fetchWithTimeout(variant.url, variant.init, variant.label.startsWith("BALANCE_") ? 3200 : 4200);
      const raw = await response.text();
      const parsed = raw ? JSON.parse(raw) as { room?: RoomSnapshot | null; error?: string; detail?: string } : null;
      if (response.ok) {
        return { data: parsed, attempts, okLabel: variant.label, errorCode: null, errorDetail: null, errorStatus: null, errorPayload: null };
      }
      const detail = parsed?.detail ?? parsed?.error ?? (raw.slice(0, 180) || "empty");
      errorCode = parsed?.error ?? errorCode;
      errorDetail = detail ?? errorDetail;
      errorStatus = response.status;
      errorPayload = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
      attempts.push(`${variant.label}:${response.status}:${parsed?.error ?? detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`${variant.label}:exception:${message}`);
    }
  }

  return { data: null, attempts, okLabel: null, errorCode, errorDetail, errorStatus, errorPayload };
}
