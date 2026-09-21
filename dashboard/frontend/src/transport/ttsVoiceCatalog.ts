import { fetchDashboardJson } from "./httpClient";

export interface EdgeVoice { value: string; label: string; locale: string }
export interface EdgeVoiceCatalog { voices: EdgeVoice[]; status: "ready" | "cached" | "unavailable" }

export async function fetchEdgeVoiceCatalog(signal: AbortSignal, refresh = false): Promise<EdgeVoiceCatalog> {
  const result = await fetchDashboardJson<EdgeVoiceCatalog & { ok: boolean }>(`/dashboard/tts/voices${refresh ? "?refresh=1" : ""}`, { signal }, 10000);
  if (!result.ok || !Array.isArray(result.voices)) throw new Error("voice_catalog_unavailable");
  return result;
}
