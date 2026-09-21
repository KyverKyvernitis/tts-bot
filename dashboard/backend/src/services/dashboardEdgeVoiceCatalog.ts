import { readFile, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TTS_VOICE_OPTIONS } from "../config/dashboardCatalogShared.js";

export interface DashboardEdgeVoice { value: string; label: string; locale: string }
export interface DashboardEdgeCatalog { voices: DashboardEdgeVoice[]; status: "ready" | "cached" | "unavailable" }

export function parseDashboardEdgeVoices(payload: unknown): DashboardEdgeVoice[] {
  if (!payload || typeof payload !== "object" || !("voices" in payload) || !Array.isArray(payload.voices)) return [];
  const names = new Map(TTS_VOICE_OPTIONS.map(option => [option.value, option.label.split(" — ")[0]]));
  const seen = new Set<string>();
  return payload.voices.slice(0, 10000).flatMap((raw: unknown) => {
    if (typeof raw !== "string" || raw.length > 120 || !/^[a-z]{2,3}(?:-[A-Za-z0-9]+)+$/.test(raw) || seen.has(raw)) return [];
    const split = raw.lastIndexOf("-");
    const locale = raw.slice(0, split);
    if (!/^[a-z]{2,3}-/.test(locale)) return [];
    seen.add(raw);
    const label = names.get(raw) || raw.slice(split + 1).replace(/Neural$/, "").replace(/Multilingual$/, " · multilíngue");
    return [{ value: raw, label, locale }];
  }).sort((a, b) => a.locale.localeCompare(b.locale) || a.label.localeCompare(b.label, "pt-BR"));
}

export function dashboardEdgeVoiceCachePath(): string {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  return resolve(root, process.env.TTS_TEMP_DIR?.trim() || "tmp_audio", "edge-voices.json");
}

export function createDashboardEdgeVoiceLoader(path = dashboardEdgeVoiceCachePath, now = Date.now) {
  let cached: DashboardEdgeCatalog = { voices: [], status: "unavailable" };
  let cachedPath = "";
  let expiresAt = 0;
  let pending: Promise<DashboardEdgeCatalog> | null = null;
  return async function load(refresh = false): Promise<DashboardEdgeCatalog> {
    const filename = path();
    if (pending) return pending;
    if (cachedPath !== filename) { cached = { voices: [], status: "unavailable" }; expiresAt = 0; cachedPath = filename; }
    if (!refresh && now() < expiresAt) return cached;
    pending = (async () => {
      try {
        if ((await stat(filename)).size > 2 * 1024 * 1024) throw new Error("voice_catalog_too_large");
        const voices = parseDashboardEdgeVoices(JSON.parse(await readFile(filename, "utf8")));
        if (!voices.length) throw new Error("empty_voice_catalog");
        cached = { voices, status: "ready" };
      } catch {
        cached = { voices: cached.voices, status: cached.voices.length ? "cached" : "unavailable" };
      }
      expiresAt = now() + (cached.status === "ready" ? 60_000 : 5_000);
      return cached;
    })();
    try { return await pending; } finally { pending = null; }
  };
}

export const loadDashboardEdgeVoices = createDashboardEdgeVoiceLoader();
