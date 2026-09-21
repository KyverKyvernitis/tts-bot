import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { SmartSelect } from "../SmartSelect";
import { edgeLocaleLabel, edgeVoicePickerModel, voiceForEdgeLocale } from "./edgeVoiceModel";
import { useEdgeVoiceCatalog } from "./useEdgeVoiceCatalog";

interface Props { field: DashboardFieldDefinition; value: unknown; changed: boolean; guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void }
export function EdgeVoicePicker({ field, value, changed, guildOptions, onChange }: Props) {
  const catalog = useEdgeVoiceCatalog();
  const model = edgeVoicePickerModel(catalog.voices, String(value || ""), field.options);
  return <div className="osk-edge-picker" data-changed={changed || undefined}>
    <div className="osk-tts-voice-pair">
      <div><label htmlFor="tts-edge-locale">Idioma e região</label><SmartSelect id="tts-edge-locale" ariaLabel="Idioma e região do Edge" presentation="anchored" value={model.locale} options={model.languages} onChange={locale => {
        if (locale === model.locale) return;
        const next = voiceForEdgeLocale(catalog.voices, locale);
        if (next !== null) onChange(field, next);
      }} /></div>
      <div><label htmlFor={`field-${field.id}`}>Voz</label><DashboardFieldControl field={{ ...field, options: model.options }} value={value} guildOptions={guildOptions} onChange={onChange} /><small className="osk-tts-resolved">{model.name} · {edgeLocaleLabel(model.locale)}</small></div>
    </div>
    {catalog.loading ? <div className="osk-tts-catalog-note" role="status">Carregando idiomas e vozes...</div> : catalog.status !== "ready" ? <div className="osk-tts-catalog-note" role="status"><span>{catalog.status === "cached" ? "Usando a última lista de vozes disponível." : "Catálogo indisponível. Sua voz atual está preservada."}</span><button type="button" onClick={catalog.retry}>Tentar novamente</button></div> : model.missing ? <div className="osk-tts-catalog-note" role="status">A voz atual não está no catálogo e será mantida até você escolher outra.</div> : null}
  </div>;
}
