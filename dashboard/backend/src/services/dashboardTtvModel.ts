import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { isPlainObject } from "../config/dashboardObjectUtils.js";

export interface DashboardTtvVoice {
  id: string;
  name: string;
  languages: string[];
  nativePortuguese: boolean;
}
export interface DashboardTtvPreferences {
  ttv_voice_id: string;
  ttv_language: string;
  ttv_pitch_semitones: string;
  ttv_speech_rate: number;
}
export interface DashboardTtvCatalog {
  version: number;
  defaultVoiceId: string;
  defaultLanguage: string;
  defaultPitchSemitones: string;
  defaultSpeechRate: number;
  voices: DashboardTtvVoice[];
}
export class DashboardTtvValueError extends Error {
  constructor(readonly field: string, message: string) {
    super(message);
    this.name = "DashboardTtvValueError";
  }
}
export const DASHBOARD_TTV_FIELD_IDS = new Set(["tts.ttv_voice_id", "tts.ttv_language", "tts.ttv_pitch_semitones", "tts.ttv_speech_rate"]);
const LEGACY_VOICES: Record<string, string> = {
  teto: "kasane-teto", kasane_teto: "kasane-teto", "kasane-teto-standard": "kasane-teto", "kasane-teto-english-cvvc": "kasane-teto",
};
let catalogCache: DashboardTtvCatalog | null = null;

export function loadDashboardTtvCatalog(): DashboardTtvCatalog {
  if (!catalogCache) {
    const candidates = [
      fileURLToPath(new URL("../data/ttv_catalog.json", import.meta.url)),
      fileURLToPath(new URL("../../../../shared/ttv_catalog.json", import.meta.url)),
    ];
    const path = candidates.find(candidate => existsSync(candidate));
    if (!path) throw new Error("ttv_catalog_missing");
    const raw = JSON.parse(readFileSync(path, "utf8")) as DashboardTtvCatalog;
    if (raw.version !== 1 || !Array.isArray(raw.voices) || !raw.voices.length
      || !raw.voices.every(voice => typeof voice.id === "string" && typeof voice.name === "string"
        && Array.isArray(voice.languages) && voice.languages.every(language => ["pt-BR", "en-US"].includes(language)))
      || !raw.voices.some(voice => voice.id === raw.defaultVoiceId && voice.languages.includes(raw.defaultLanguage))) {
      throw new Error("ttv_catalog_invalid");
    }
    catalogCache = raw;
  }
  return structuredClone(catalogCache);
}

export function normalizeTtvVoice(value: unknown, catalog = loadDashboardTtvCatalog()): string {
  const raw = String(value ?? "").trim().toLowerCase();
  const id = LEGACY_VOICES[raw] || raw || catalog.defaultVoiceId;
  if (!catalog.voices.some(voice => voice.id === id)) throw new DashboardTtvValueError("ttv_voice_id", "Escolha uma das vozes do TextToVocaloid.");
  return id;
}
export function normalizeTtvLanguage(value: unknown): string {
  const raw = String(value ?? "").trim().toLowerCase().replace(/_/g, "-");
  if (["", "pt", "pt-br"].includes(raw)) return "pt-BR";
  if (["en", "en-us"].includes(raw)) return "en-US";
  throw new DashboardTtvValueError("ttv_language", "Escolha português ou inglês para o TextToVocaloid.");
}
export function normalizeTtvPitch(value: unknown): string {
  const raw = String(value ?? "0").trim().toLowerCase().replace(",", ".").replace(/[−–—]/g, "-").replace(/(?:semitones|semitone|semitons|semitom|st)$/, "").trim();
  const number = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(raw || "0") ? Number(raw || "0") : Number.NaN;
  if (typeof value === "boolean" || !Number.isFinite(number) || number < -4 || number > 4 || Math.abs(number * 2 - Math.round(number * 2)) > 2e-9) {
    throw new DashboardTtvValueError("ttv_pitch_semitones", "O tom deve ficar entre −4 e +4 semitons, em passos de 0,5.");
  }
  return `${number >= 0 || Object.is(number, -0) ? "+" : ""}${(number || 0).toFixed(1)}`;
}
function decimalNumber(text: string): number {
  return /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text) ? Number(text) : Number.NaN;
}
export function normalizeTtvRate(value: unknown): number {
  const number = value === null || value === undefined || typeof value === "string" && !value.trim()
    ? 1 : decimalNumber(String(value).trim().replace(",", "."));
  if (typeof value === "boolean" || !Number.isFinite(number) || number < 0.5 || number > 1.5) {
    throw new DashboardTtvValueError("ttv_speech_rate", "A velocidade deve ficar entre 50% e 150%.");
  }
  return number;
}
/** Resolve only server defaults. Personal preferences remain in the bot's user settings. */
export function resolveDashboardTtvDefaults(guild: Record<string, unknown> | null): DashboardTtvPreferences {
  const defaults = isPlainObject(guild?.tts_defaults) ? guild.tts_defaults : {};
  const catalog = loadDashboardTtvCatalog();
  let voice: string;
  let language: string;
  // Keep unrecognised stored choices visible until an administrator replaces them.
  try { voice = normalizeTtvVoice(defaults.ttv_voice_id, catalog); } catch { voice = String(defaults.ttv_voice_id ?? "").trim(); }
  try { language = normalizeTtvLanguage(defaults.ttv_language); } catch { language = String(defaults.ttv_language ?? "").trim(); }
  let pitch = catalog.defaultPitchSemitones;
  for (const candidate of [defaults.ttv_pitch_semitones, defaults.teto_pitch_semitones, process.env.TTS_TETO_DEFAULT_PITCH_SEMITONES]) {
    if (candidate === undefined || candidate === null || typeof candidate === "string" && !candidate.trim()) continue;
    try { pitch = normalizeTtvPitch(candidate); break; } catch { /* Try the legacy value, then the configured default. */ }
  }
  let rate = catalog.defaultSpeechRate;
  try { rate = normalizeTtvRate(defaults.ttv_speech_rate); } catch { /* Match the bot's fallback for a malformed stored rate. */ }
  return { ttv_voice_id: voice, ttv_language: language, ttv_pitch_semitones: pitch, ttv_speech_rate: rate };
}

export function normalizeDashboardTtvField(fieldId: string, raw: unknown): unknown {
  switch (fieldId) {
    case "tts.ttv_voice_id":
      if (typeof raw !== "string") throw new DashboardTtvValueError("ttv_voice_id", "Escolha uma voz válida.");
      return normalizeTtvVoice(raw);
    case "tts.ttv_language":
      if (typeof raw !== "string") throw new DashboardTtvValueError("ttv_language", "Escolha um idioma válido.");
      return normalizeTtvLanguage(raw);
    case "tts.ttv_pitch_semitones":
      if (typeof raw !== "string" && typeof raw !== "number") throw new DashboardTtvValueError("ttv_pitch_semitones", "Informe um tom válido.");
      return normalizeTtvPitch(raw);
    case "tts.ttv_speech_rate":
      if (typeof raw !== "number") throw new DashboardTtvValueError("ttv_speech_rate", "Informe uma velocidade válida.");
      return normalizeTtvRate(raw);
    default:
      throw new DashboardTtvValueError(fieldId, "Esta opção não pertence ao TextToVocaloid.");
  }
}

export function validateDashboardTtvVoiceLanguage(preferences: DashboardTtvPreferences): void {
  const voice = loadDashboardTtvCatalog().voices.find(item => item.id === preferences.ttv_voice_id);
  if (!voice) throw new DashboardTtvValueError("ttv_voice_id", "A voz salva não está no catálogo. Escolha uma voz antes de salvar.");
  if (!voice.languages.includes(preferences.ttv_language)) {
    throw new DashboardTtvValueError("ttv_language", voice.languages.length === 1
      ? `${voice.name} está disponível somente em português brasileiro.`
      : `Escolha português ou inglês para ${voice.name}.`);
  }
}
