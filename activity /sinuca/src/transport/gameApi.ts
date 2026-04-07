import type { GameSnapshot, RoomSnapshot } from "../types/activity";
import {
  appendNoStoreNonce,
  buildQueryStringFromPayload,
  fetchWithTimeout,
  resolveApiCandidates,
  resolveLegacyBalanceAction,
} from "./httpClient";
import type { HttpTransportResult } from "./lobbyApi";

export async function fetchGameStateRequest(roomId: string, sinceSeq = 0): Promise<HttpTransportResult<{ game?: GameSnapshot | null; error?: string }>> {
  const attempts: string[] = [];

  for (const baseUrl of resolveApiCandidates(`/games/${roomId}`)) {
    try {
      const url = new URL(baseUrl, window.location.origin);
      if (sinceSeq > 0) url.searchParams.set("sinceSeq", String(sinceSeq));
      const response = await fetchWithTimeout(url.toString(), { method: "GET", credentials: "same-origin" }, 3200);
      const raw = await response.text();
      const parsed = raw ? JSON.parse(raw) as { game?: GameSnapshot | null; error?: string } : null;
      if (response.ok) {
        return { data: parsed, attempts, okLabel: baseUrl };
      }
      attempts.push(`${url.toString()}:${response.status}:${(parsed?.error ?? raw.slice(0, 180)) || "empty"}`);
    } catch (error) {
      attempts.push(`${baseUrl}:exception:${error instanceof Error ? error.message : "unknown"}`);
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
      const parsed = raw ? JSON.parse(raw) as { game?: GameSnapshot | null; room?: RoomSnapshot | null; error?: string; detail?: string } : null;
      if (response.ok) {
        return { data: parsed, attempts, okLabel: variant.label };
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
