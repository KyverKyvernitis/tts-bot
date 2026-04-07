import type { BalanceDebugSnapshot, BalanceSnapshot } from "../types/activity";
import { fetchWithTimeout, resolveApiCandidates } from "./httpClient";

export type BalanceRequestResult = {
  data: { balance?: BalanceSnapshot; debug?: BalanceDebugSnapshot; error?: string; detail?: string } | null;
  attempts: string[];
  okLabel: string | null;
};

export async function fetchBalanceRequest(params: {
  guildId: string;
  userId: string;
}): Promise<BalanceRequestResult> {
  const attempts: string[] = [];

  for (const baseUrl of resolveApiCandidates("/balance")) {
    const requestUrl = `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}guildId=${encodeURIComponent(params.guildId)}&userId=${encodeURIComponent(params.userId)}`;
    try {
      const response = await fetchWithTimeout(requestUrl, {
        method: "GET",
        credentials: "same-origin",
      }, 3500);
      const raw = await response.text();
      let parsed: { balance?: BalanceSnapshot; debug?: BalanceDebugSnapshot; error?: string; detail?: string } | null = null;
      try {
        parsed = raw ? JSON.parse(raw) as { balance?: BalanceSnapshot; debug?: BalanceDebugSnapshot; error?: string; detail?: string } : null;
      } catch {
        parsed = null;
      }

      if (response.ok && parsed?.balance && parsed?.debug) {
        return {
          data: parsed,
          attempts,
          okLabel: baseUrl,
        };
      }

      const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
      attempts.push(`${baseUrl}:${response.status}:${detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`${baseUrl}:exception:${message}`);
    }
  }

  return { data: null, attempts, okLabel: null };
}
