import { DashboardConfigValueError } from "./dashboardTypes.js";
import { getPath } from "./dashboardObjectUtils.js";

export const DASHBOARD_PREFIX_FIELD_IDS = new Set([
  "general.bot_prefix",
  "tts.atts_prefix",
  "tts.teto_prefix",
  "tts.gtts_prefix",
  "tts.edge_prefix",
]);

export function normalizeDashboardPrefix(raw: unknown, maxLength = 8): string {
  return String(raw ?? "").trim().slice(0, maxLength);
}

export function isDashboardTimeZone(value: unknown): value is string {
  const timeZone = String(value ?? "").trim();
  if (!timeZone || timeZone.length > 64) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone }).format(0);
    return true;
  } catch {
    return false;
  }
}

export function normalizeDashboardTimeZone(raw: unknown): string {
  const timeZone = String(raw ?? "").trim();
  if (!isDashboardTimeZone(timeZone)) {
    throw new DashboardConfigValueError("general.timezone", "Escolha um fuso horário válido.");
  }
  return timeZone;
}

export function validateDashboardPrefixes(guild: Record<string, unknown>): void {
  const entries: Array<[string, string]> = [
    ["bot", normalizeDashboardPrefix(getPath(guild, "bot_prefix"))],
    ["ATTS", normalizeDashboardPrefix(getPath(guild, "atts_prefix"))],
    ["Kasane Teto", normalizeDashboardPrefix(getPath(guild, "teto_prefix"))],
    ["gTTS", normalizeDashboardPrefix(getPath(guild, "gtts_prefix"))],
    ["Edge", normalizeDashboardPrefix(getPath(guild, "edge_prefix"))],
  ];
  const seen = new Map<string, string>();
  for (const [label, value] of entries) {
    if (!value) throw new Error(`O prefixo de ${label} não pode ficar vazio.`);
    if (/\s/u.test(value) || Array.from(value).some((character) => character.codePointAt(0)! < 33)) {
      throw new Error(`O prefixo de ${label} não pode conter espaços ou caracteres invisíveis.`);
    }
    const other = seen.get(value);
    if (other) throw new Error(`Os prefixos de ${other} e ${label} não podem ser iguais (${value}).`);
    seen.set(value, label);
  }
}
