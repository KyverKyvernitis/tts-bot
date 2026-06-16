import type { DashboardBootstrapPayload, DashboardSettingsPayload, DashboardSummaryPayload } from "../types/dashboard";
import { fetchJsonFromCandidates, resolveApiCandidates, resolveStrictApiCandidates } from "./httpClient";

function authHeaders(accessToken: string): HeadersInit {
  return { Authorization: `Bearer ${accessToken}` };
}

export async function fetchDashboardBootstrap(accessToken: string, guildId: string): Promise<DashboardBootstrapPayload> {
  const query = `guild_id=${encodeURIComponent(guildId)}`;
  return await fetchJsonFromCandidates<DashboardBootstrapPayload>(
    resolveStrictApiCandidates(`/dashboard/bootstrap?${query}`),
    { method: "GET", headers: authHeaders(accessToken) },
    6000,
  );
}

export async function fetchDashboardSummary(accessToken: string, guildId: string): Promise<DashboardSummaryPayload> {
  return await fetchJsonFromCandidates<DashboardSummaryPayload>(
    resolveStrictApiCandidates(`/dashboard/guild/${encodeURIComponent(guildId)}/summary`),
    { method: "GET", headers: authHeaders(accessToken) },
    6000,
  );
}

export async function fetchDashboardSettings(accessToken: string, guildId: string): Promise<DashboardSettingsPayload> {
  return await fetchJsonFromCandidates<DashboardSettingsPayload>(
    resolveStrictApiCandidates(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`),
    { method: "GET", headers: authHeaders(accessToken) },
    6000,
  );
}

export async function patchDashboardSettings(accessToken: string, guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[] }> {
  return await fetchJsonFromCandidates<{ ok: true; values: Record<string, unknown>; saved: string[] }>(
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
  );
}

export function resolveDashboardHealthCandidates() {
  return resolveApiCandidates("/health");
}
