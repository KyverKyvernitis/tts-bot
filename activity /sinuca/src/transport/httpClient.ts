export const DEFAULT_PUBLIC_HOST = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || "osakaagiota.duckdns.org";

export function joinBaseAndPath(base: string, path: string) {
  const normalizedBase = base.endsWith("/") ? base.slice(0, -1) : base;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

export function resolvePublicBaseCandidates() {
  const configuredApiBase = (import.meta.env.VITE_SINUCA_API_BASE_URL as string | undefined)?.trim();
  const configuredPublicHost = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim();
  const candidates: string[] = [window.location.origin];

  if (configuredApiBase) {
    candidates.push(configuredApiBase);
  }

  const directHost = configuredPublicHost || DEFAULT_PUBLIC_HOST;
  if (directHost) {
    const withScheme = /^https?:\/\//i.test(directHost) ? directHost : `https://${directHost}`;
    candidates.push(withScheme);
  }

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function resolveApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const candidates: string[] = [`/api${normalizedPath}`];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, `/api${normalizedPath}`));
  }

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, normalizedPath));
  }

  candidates.push(normalizedPath);
  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function resolveStrictApiCandidates(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiPath = normalizedPath.startsWith("/api/") || normalizedPath === "/api"
    ? normalizedPath
    : `/api${normalizedPath}`;
  const candidates: string[] = [apiPath];

  for (const base of resolvePublicBaseCandidates()) {
    candidates.push(joinBaseAndPath(base, apiPath));
  }

  return candidates.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function buildQueryStringFromPayload(payload: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || value === null) continue;
    if (typeof value === "boolean") {
      params.set(key, value ? "true" : "false");
      continue;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) continue;
      params.set(key, `${value}`);
      continue;
    }
    params.set(key, String(value));
  }
  return params.toString();
}

export function resolveLegacyBalanceAction(path: string) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (normalizedPath === "/rooms") return "rooms_list";
  if (normalizedPath === "/rooms/create") return "room_create";
  if (normalizedPath === "/rooms/join") return "room_join";
  if (normalizedPath === "/rooms/leave") return "room_leave";
  if (normalizedPath === "/rooms/ready") return "room_ready";
  if (normalizedPath === "/rooms/stake") return "room_stake";
  if (normalizedPath === "/games/start") return "game_start";
  if (normalizedPath === "/games/shoot") return "game_shoot";
  if (normalizedPath === "/games/rematch") return "game_rematch";
  if (normalizedPath === "/games/debug") return "ui_debug";
  if (/^\/rooms\/[^/]+$/.test(normalizedPath)) return "room_get";
  return null;
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

export function appendNoStoreNonce(urlLike: string | URL, nonce?: string) {
  const url = new URL(urlLike.toString(), window.location.origin);
  url.searchParams.set("_rt", nonce ?? `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
  return url;
}

export function dispatchLeaveBeacon(roomId: string, userId: string, closeRoom: boolean) {
  const payload = new URLSearchParams();
  payload.set('roomId', roomId);
  payload.set('userId', userId);
  payload.set('closeRoom', String(closeRoom));
  payload.set('reason', closeRoom ? 'activity_unload_close' : 'activity_unload_leave');

  for (const baseUrl of resolveStrictApiCandidates('/rooms/leave')) {
    try {
      if (typeof navigator.sendBeacon === 'function') {
        const blob = new Blob([payload.toString()], { type: 'application/x-www-form-urlencoded;charset=UTF-8' });
        if (navigator.sendBeacon(baseUrl, blob)) return true;
      }
    } catch {
      // ignore and continue with keepalive fallback
    }
  }

  for (const baseUrl of resolveStrictApiCandidates('/rooms/leave')) {
    try {
      void fetch(baseUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
        body: payload.toString(),
        credentials: 'same-origin',
        keepalive: true,
      });
      return true;
    } catch {
      // ignore and keep trying other candidates
    }
  }

  return false;
}
