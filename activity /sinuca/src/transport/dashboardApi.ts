import type { DashboardBootstrapPayload, DashboardSettingsPayload, DashboardSummaryPayload } from "../types/dashboard";
import { DashboardHttpError, fetchJsonFromCandidates, resolveStrictApiCandidates, resolveTokenCandidates } from "./httpClient";

function authHeaders(accessToken: string): HeadersInit {
  return { Authorization: `Bearer ${accessToken}` };
}

function shouldFallbackToTokenRpc(error: unknown): boolean {
  if (error instanceof DashboardHttpError) {
    if (error.code === "html_instead_of_json") return true;
    if (error.code === "invalid_json") return true;
    if (error.status === 0 || error.status === 404 || error.status === 405) return true;
    return false;
  }
  return true;
}

async function callDashboardTokenRpc<T>(accessToken: string, guildId: string, action: string, extraBody: Record<string, unknown> = {}): Promise<T> {
  return await fetchJsonFromCandidates<T>(
    resolveTokenCandidates(),
    {
      method: "POST",
      headers: {
        ...authHeaders(accessToken),
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify({
        dashboard_action: action,
        guild_id: guildId,
        ...extraBody,
      }),
    },
    7000,
  );
}

async function withTokenRpcPrimary<T>(apiCall: () => Promise<T>, accessToken: string, guildId: string, action: string, extraBody: Record<string, unknown> = {}): Promise<T> {
  try {
    return await callDashboardTokenRpc<T>(accessToken, guildId, action, extraBody);
  } catch (rpcError) {
    if (rpcError instanceof DashboardHttpError && rpcError.code === "http_error" && (rpcError.status === 401 || rpcError.status === 403)) {
      throw rpcError;
    }

    try {
      return await apiCall();
    } catch (apiError) {
      if (!shouldFallbackToTokenRpc(apiError)) throw apiError;
      throw rpcError;
    }
  }
}

export async function fetchDashboardBootstrap(accessToken: string, guildId: string): Promise<DashboardBootstrapPayload> {
  const query = `guild_id=${encodeURIComponent(guildId)}`;
  return await withTokenRpcPrimary(
    () => fetchJsonFromCandidates<DashboardBootstrapPayload>(
      resolveStrictApiCandidates(`/dashboard/bootstrap?${query}`),
      { method: "GET", headers: authHeaders(accessToken) },
      6000,
    ),
    accessToken,
    guildId,
    "bootstrap",
  );
}

export async function fetchDashboardSummary(accessToken: string, guildId: string): Promise<DashboardSummaryPayload> {
  return await withTokenRpcPrimary(
    () => fetchJsonFromCandidates<DashboardSummaryPayload>(
      resolveStrictApiCandidates(`/dashboard/guild/${encodeURIComponent(guildId)}/summary`),
      { method: "GET", headers: authHeaders(accessToken) },
      6000,
    ),
    accessToken,
    guildId,
    "summary",
  );
}

export async function fetchDashboardSettings(accessToken: string, guildId: string): Promise<DashboardSettingsPayload> {
  return await withTokenRpcPrimary(
    () => fetchJsonFromCandidates<DashboardSettingsPayload>(
      resolveStrictApiCandidates(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`),
      { method: "GET", headers: authHeaders(accessToken) },
      6000,
    ),
    accessToken,
    guildId,
    "settings",
  );
}

export async function patchDashboardSettings(accessToken: string, guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[] }> {
  return await withTokenRpcPrimary(
    () => fetchJsonFromCandidates<{ ok: true; values: Record<string, unknown>; saved: string[] }>(
      resolveStrictApiCandidates(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`),
      {
        method: "PATCH",
        headers: {
          ...authHeaders(accessToken),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ updates }),
      },
      8000,
    ),
    accessToken,
    guildId,
    "settings:update",
    { updates },
  );
}

export function resolveDashboardHealthCandidates() {
  return resolveStrictApiCandidates("/health");
}
