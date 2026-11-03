import { AudioLines, Languages, Mic2, RotateCcw, Type } from "lucide-react";
import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { FieldsPanel } from "../SectionEditorPanels";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { sectionEditorValuesEqual } from "../sectionEditorModel";
import { AdvancedSettings } from "./AdvancedSettings";
import { EdgeVoicePicker } from "./EdgeVoicePicker";

interface Props {
  fields: DashboardFieldDefinition[]; values: Record<string, unknown>; draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void;
}
const engines = [
  { id: "edge", label: "Microsoft Edge", description: "Voz, idioma, velocidade e tom", icon: AudioLines, prefix: "tts.edge_prefix", fallback: ",", fields: ["tts.voice", "tts.rate", "tts.pitch"] },
  { id: "gtts", label: "Google TTS", description: "Leitura no idioma escolhido", icon: Languages, prefix: "tts.gtts_prefix", fallback: ".", fields: ["tts.language"] },
];

function withDefaultOption(field: DashboardFieldDefinition, draft: Record<string, unknown>): DashboardFieldDefinition {
  if (field.id !== "tts.language") return field;
  const options = [{ value: "", label: "Português — padrão do bot" }, ...(field.options || []).filter(option => option.value !== "")];
  const current = String(draft[field.id] || "");
  if (current && !options.some(option => option.value === current)) {
    const normalized = current.replace(/_/g, "-").toLowerCase();
    const language = options.find(option => option.value.toLowerCase() === normalized)
      || options.find(option => option.value === normalized.split("-")[0]);
    if (language) options.push({ value: current, label: `${language.label} — configuração atual` });
  }
  return { ...field, options };
}

function adjustmentDefault(field: DashboardFieldDefinition): string {
  return field.id === "tts.rate" ? "+0%" : "+0Hz";
}

export function TtsVoiceSettings({ fields, values, draft, guildOptions, onChange }: Props) {
  const engineFields = new Set(engines.flatMap(engine => [...engine.fields, engine.prefix]));
  const otherPrefixes = fields.filter(field => ["tts.atts_prefix", "tts.teto_prefix"].includes(field.id));
  for (const field of otherPrefixes) engineFields.add(field.id);
  const shared = fields.filter(field => field.id !== "tts.engine" && !engineFields.has(field.id));
  const common = { values, draft, guildOptions, onChange };
  const prefixSummary = otherPrefixes.map(field => {
    const value = String(draft[field.id] ?? field.placeholder ?? "").trim();
    const label = field.id === "tts.atts_prefix" ? "ATTS" : "Kasane Teto";
    return value ? `${label}: ${value}` : label;
  }).join(" · ");

  return <div className="osk-tts-settings">
    <div className="osk-tts-engines">{engines.map(engine => {
      const Icon = engine.icon;
      const prefix = fields.find(field => field.id === engine.prefix);
      const voice = engine.id === "edge" ? fields.find(field => field.id === "tts.voice") : undefined;
      const controls = engine.fields.filter(id => id !== "tts.voice").map(id => fields.find(field => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field));
      const languageControls = controls.filter(field => !["tts.rate", "tts.pitch"].includes(field.id)).map(field => withDefaultOption(field, draft));
      const adjustments = controls.filter(field => ["tts.rate", "tts.pitch"].includes(field.id));
      const prefixText = prefix ? String(draft[prefix.id] ?? engine.fallback) : engine.fallback;

      return <section key={engine.id} className="osk-settings-card osk-tts-engine" data-engine={engine.id}>
        <header>
          <span className="osk-tts-engine-title"><Icon size={21} aria-hidden="true" /><span><h2>{engine.label}</h2><small>{engine.description}</small></span></span>
          {prefix && <div className="osk-tts-prefix" data-changed={!sectionEditorValuesEqual(draft[prefix.id], values[prefix.id]) || undefined}>
            <label htmlFor={`field-${prefix.id}`}>Prefixo</label>
            <DashboardFieldControl field={prefix} value={draft[prefix.id] ?? engine.fallback} guildOptions={guildOptions} onChange={onChange} />
          </div>}
        </header>
        <p className="osk-tts-engine-note">{prefixText.trim() ? <><code>{prefixText}oi, gente!</code><span>Use o prefixo antes do texto no chat.</span></> : "Defina o prefixo usado por este provedor."}</p>
        {voice && <EdgeVoicePicker field={voice} value={draft[voice.id]} changed={!sectionEditorValuesEqual(draft[voice.id], values[voice.id])} guildOptions={guildOptions} onChange={onChange} />}
        {languageControls.length > 0 && <FieldsPanel {...common} sectionId="tts" fields={languageControls} />}
        {adjustments.length > 0 && <div className="osk-tts-adjustments">{adjustments.map(field => {
          const current = draft[field.id] ?? adjustmentDefault(field);
          const numeric = Number.parseInt(String(current), 10);
          const atDefault = Number.isFinite(numeric) && numeric === 0;
          return <div className="osk-tts-adjustment" key={field.id} data-changed={!sectionEditorValuesEqual(draft[field.id], values[field.id]) || undefined}>
            <header><label htmlFor={`field-${field.id}`}>{field.label}</label><button type="button" className="osk-tts-reset" onClick={() => onChange(field, adjustmentDefault(field))} disabled={atDefault} aria-label={`Restaurar ${field.label.toLocaleLowerCase("pt-BR")} padrão`}><RotateCcw size={13} aria-hidden="true" />Padrão</button></header>
            <DashboardFieldControl field={field} value={current} guildOptions={guildOptions} onChange={onChange} />
          </div>;
        })}</div>}
      </section>;
    })}</div>
    {shared.length > 0 && <section className="osk-settings-card osk-tts-destination"><header><Mic2 size={19} aria-hidden="true" /><h2>Canal de voz</h2></header><FieldsPanel {...common} sectionId="tts" fields={shared} /></section>}
    {otherPrefixes.length > 0 && <AdvancedSettings title="Outros prefixos" summary={prefixSummary} fields={otherPrefixes} values={values} draft={draft} icon={Type} className="osk-tts-extra-prefixes">
      <FieldsPanel {...common} sectionId="tts" fields={otherPrefixes.map(field => field.id === "tts.teto_prefix" ? { ...field, description: "Prefixo para a voz Kasane Teto, quando disponível." } : field)} />
    </AdvancedSettings>}
  </div>;
}
