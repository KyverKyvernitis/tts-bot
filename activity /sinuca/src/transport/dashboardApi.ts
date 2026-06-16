import type { DashboardBootstrapPayload, DashboardSettingsPayload, DashboardSummaryPayload } from "../types/dashboard";
import { fetchWithTimeout, resolveApiCandidates, resolveStrictApiCandidates } from "./httpClient";

async function fetchJson<T>(url: string, init: RequestInit, timeoutMs = 5000): Promise<T> {
  const response = await fetchWithTimeout(url, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      ...(init.headers ?? {}),
      "Accept": "application/json",
    },
  }, timeoutMs);
  const raw = await response.text();
  let parsed: unknown = null;
  try {
    parsed = raw ? JSON.parse(raw) : null;
  } catch {
    parsed = { ok: false, error: raw.slice(0, 160) || "invalid_json" };
  }
  if (!response.ok) {
    const message = typeof parsed === "object" && parsed && "error" in parsed ? String((parsed as { error?: unknown }).error || "request_failed") : "request_failed";
    throw new Error(`${message} (${response.status})`);
  }
  return parsed as T;
}

function firstApiUrl(path: string): string {
  return resolveStrictApiCandidates(path)[0] ?? path;
}

export async function fetchDashboardBootstrap(accessToken: string, guildId: string): Promise<DashboardBootstrapPayload> {
  const url = firstApiUrl(`/dashboard/bootstrap?guild_id=${encodeURIComponent(guildId)}`);
  return await fetchJson<DashboardBootstrapPayload>(url, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  }, 5000);
}

export async function fetchDashboardSummary(accessToken: string, guildId: string): Promise<DashboardSummaryPayload> {
  const url = firstApiUrl(`/dashboard/guild/${encodeURIComponent(guildId)}/summary`);
  return await fetchJson<DashboardSummaryPayload>(url, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  }, 5000);
}

export async function fetchDashboardSettings(accessToken: string, guildId: string): Promise<DashboardSettingsPayload> {
  const url = firstApiUrl(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`);
  return await fetchJson<DashboardSettingsPayload>(url, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  }, 5000);
}

export async function patchDashboardSettings(accessToken: string, guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[] }> {
  const url = firstApiUrl(`/dashboard/guild/${encodeURIComponent(guildId)}/settings`);
  return await fetchJson<{ ok: true; values: Record<string, unknown>; saved: string[] }>(url, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ updates }),
  }, 7000);
}

export function resolveDashboardHealthCandidates() {
  return resolveApiCandidates("/health");
}
