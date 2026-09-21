/// <reference lib="es2021.intl" />
import type { EdgeVoice } from "../../transport/ttsVoiceCatalog";
import type { DashboardFieldOption } from "../../types/dashboard";

export const EDGE_DEFAULT_VOICE = "pt-BR-FranciscaNeural";
export function edgeVoiceLocale(value: string): string {
  const voice = value || EDGE_DEFAULT_VOICE;
  return voice.includes("-") ? voice.slice(0, voice.lastIndexOf("-")) : "pt-BR";
}
export function edgeLocaleLabel(locale: string): string {
  try {
    const label = new Intl.DisplayNames(["pt-BR"], { type: "language" }).of(locale) || locale;
    return label.charAt(0).toLocaleUpperCase("pt-BR") + label.slice(1);
  } catch { return locale; }
}
export function edgeVoiceName(value: string): string {
  return (value || EDGE_DEFAULT_VOICE).split("-").slice(-1)[0].replace(/Neural$/, "").replace(/Multilingual$/, " · multilíngue");
}
export function edgeVoicePickerModel(voices: EdgeVoice[], current: string, fallback: DashboardFieldOption[] = []) {
  const selected = voices.find(voice => voice.value === (current || EDGE_DEFAULT_VOICE));
  const locale = selected?.locale || edgeVoiceLocale(current);
  const locales = Array.from(new Set(["pt-BR", locale, ...voices.map(voice => voice.locale)]));
  const languages = locales.map(value => ({ value, label: edgeLocaleLabel(value) })).sort((a, b) => a.label.localeCompare(b.label, "pt-BR"));
  const options: DashboardFieldOption[] = voices.filter(voice => voice.locale === locale).map(({ value, label }) => ({ value, label }));
  if (locale === "pt-BR") options.unshift({ value: "", label: "Padrão do bot" });
  if (current && !options.some(option => option.value === current)) options.unshift({ value: current, label: fallback.find(option => option.value === current)?.label || edgeVoiceName(current) });
  return { locale, languages, options, name: selected?.label || edgeVoiceName(current), missing: Boolean(current && !selected) };
}
export function voiceForEdgeLocale(voices: EdgeVoice[], locale: string): string | null {
  return locale === "pt-BR" ? "" : voices.find(voice => voice.locale === locale)?.value ?? null;
}
