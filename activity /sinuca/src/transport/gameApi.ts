import type { GameSnapshot, RoomSnapshot } from "../types/activity";
import {
  appendNoStoreNonce,
  buildQueryStringFromPayload,
  fetchWithTimeout,
  resolveApiCandidates,
  resolveLegacyBalanceAction,
  resolveStrictApiCandidates,
} from "./httpClient";
import type { HttpTransportMeta, HttpTransportResult } from "./lobbyApi";

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
    contentType: response.headers.get('content-type'),
    responsePreview: buildResponsePreview(raw),
  };
}

export async function fetchGameStateRequest(roomId: string, sinceSeq = 0): Promise<HttpTransportResult<{ game?: GameSnapshot | null; error?: string }>> {
  const attempts: string[] = [];
  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

  for (const baseUrl of resolveStrictApiCandidates(`/rooms/${encodeURIComponent(roomId)}/game`)) {
    const url = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    if (sinceSeq > 0) url.searchParams.set('sinceSeq', String(sinceSeq));
    requestVariants.push({ label: `STRICT_ROOM_GAME:${baseUrl}`, url: url.toString(), init: { method: 'GET', credentials: 'same-origin', cache: 'no-store' } });
  }

  for (const baseUrl of resolveStrictApiCandidates(`/games/${encodeURIComponent(roomId)}`)) {
    const url = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    if (sinceSeq > 0) url.searchParams.set('sinceSeq', String(sinceSeq));
    requestVariants.push({ label: `STRICT_GAMES:${baseUrl}`, url: url.toString(), init: { method: 'GET', credentials: 'same-origin', cache: 'no-store' } });
  }

  for (const baseUrl of resolveApiCandidates('/balance')) {
    const url = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    url.searchParams.set('action', 'game_get');
    url.searchParams.set('roomId', roomId);
    if (sinceSeq > 0) url.searchParams.set('sinceSeq', String(sinceSeq));
    requestVariants.push({ label: `BALANCE_GAME_GET:${baseUrl}`, url: url.toString(), init: { method: 'GET', credentials: 'same-origin', cache: 'no-store' } });
  }

  for (const variant of requestVariants) {
    try {
      const response = await fetchWithTimeout(variant.url, variant.init, variant.label.startsWith('BALANCE_') ? 3200 : 3600);
      const raw = await response.text();
      const contentType = response.headers.get('content-type') ?? '';
      const trimmed = raw.trim();
      if (trimmed.startsWith('<') || /text\/html/i.test(contentType)) {
        attempts.push(`${variant.label}:${response.status}:html_response`);
        continue;
      }
      const parsed = parseJsonSafely<{ game?: GameSnapshot | null; error?: string }>(raw);
      if (response.ok) {
        return { data: parsed, attempts, okLabel: variant.label, okMeta: buildTransportMeta(variant.label, variant.url, response, raw) };
      }
      attempts.push(`${variant.label}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
    } catch (error) {
      attempts.push(`${variant.label}:exception:${error instanceof Error ? error.message : "unknown"}`);
    }
  }

  return { data: null, attempts, okLabel: null };
}

export async function postGameActionRequest(path: string, payload: Record<string, unknown>, reason: string): Promise<HttpTransportResult<{ game?: GameSnapshot | null; room?: RoomSnapshot | null; error?: string; detail?: string }>> {
  const attempts: string[] = [];
  const query = buildQueryStringFromPayload(payload);
  const legacyAction = resolveLegacyBalanceAction(path);
  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

  for (const baseUrl of resolveApiCandidates(path)) {
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
    if (query) {
      const getUrl = appendNoStoreNonce(baseUrl, `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
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
      console.log("[sinuca-http-action]", JSON.stringify({ path, label: variant.label, url: variant.url, reason, payload }));
      const response = await fetchWithTimeout(variant.url, variant.init, variant.label.startsWith("BALANCE_") ? 3200 : 4200);
      const raw = await response.text();
      const parsed = parseJsonSafely<{ game?: GameSnapshot | null; room?: RoomSnapshot | null; error?: string; detail?: string }>(raw);
      if (response.ok) {
        return { data: parsed, attempts, okLabel: variant.label, okMeta: buildTransportMeta(variant.label, variant.url, response, raw) };
      }
      const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
      attempts.push(`${variant.label}:${response.status}:${detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`${variant.label}:exception:${message}`);
    }
  }

  return { data: null, attempts, okLabel: null };
}
