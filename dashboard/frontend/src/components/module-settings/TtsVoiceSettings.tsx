import { AudioLines, Languages, Mic2 } from "lucide-react";
import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { FieldsPanel } from "../SectionEditorPanels";

interface Props {
  fields: DashboardFieldDefinition[]; values: Record<string, unknown>; draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void;
}
const engines = [
  { id: "edge", label: "Microsoft Edge", icon: AudioLines, prefix: "tts.edge_prefix", fallback: ",", fields: ["tts.voice", "tts.rate", "tts.pitch"] },
  { id: "gtts", label: "Google TTS", icon: Languages, prefix: "tts.gtts_prefix", fallback: ".", fields: ["tts.language"] },
];

function withDefaultOption(field: DashboardFieldDefinition, draft: Record<string, unknown>): DashboardFieldDefinition {
  if (!["tts.voice", "tts.language"].includes(field.id)) return field;
  const label = field.id === "tts.voice" ? "Francisca — padrão do bot" : "Português — padrão do bot";
  const options = [{ value: "", label }, ...(field.options || []).filter(option => option.value !== "")];
  const current = String(draft[field.id] || "");
  if (field.id === "tts.language" && current && !options.some(option => option.value === current)) {
    const language = options.find(option => option.value === current.toLowerCase().split("-")[0]);
    if (language) options.push({ value: current, label: `${language.label} — configuração atual` });
  }
  return { ...field, options };
}

export function TtsVoiceSettings({ fields, draft, ...common }: Props) {
  const engineFields = new Set(engines.flatMap(engine => engine.fields));
  const shared = fields.filter(field => field.id !== "tts.engine" && !engineFields.has(field.id));
  return <div className="osk-tts-settings">
    <div className="osk-tts-engines">{engines.map(engine => {
      const Icon = engine.icon;
      const controls = engine.fields.map(id => fields.find(field => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field)).map(field => withDefaultOption(field, draft));
      return <section key={engine.id} className="osk-settings-card osk-tts-engine" data-engine={engine.id}>
        <header><Icon size={21} aria-hidden="true" /><h2>{engine.label}</h2></header>
        <p className="osk-tts-prefix">Prefixo <code>{String(draft[engine.prefix] || engine.fallback)}</code></p>
        <FieldsPanel {...common} sectionId="tts" fields={controls} draft={draft} />
      </section>;
    })}</div>
    {shared.length > 0 && <section className="osk-settings-card"><header><Mic2 size={19} aria-hidden="true" /><h2>Canal de voz</h2></header><FieldsPanel {...common} sectionId="tts" fields={shared} draft={draft} /></section>}
  </div>;
}
