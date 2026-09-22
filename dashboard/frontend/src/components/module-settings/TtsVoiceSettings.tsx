import { AudioLines, Languages, Mic2, Type } from "lucide-react";
import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { FieldsPanel } from "../SectionEditorPanels";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { EdgeVoicePicker } from "./EdgeVoicePicker";

interface Props {
  fields: DashboardFieldDefinition[]; values: Record<string, unknown>; draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void;
}
const engines = [
  { id: "edge", label: "Microsoft Edge", icon: AudioLines, prefix: "tts.edge_prefix", fallback: ",", fields: ["tts.voice", "tts.rate", "tts.pitch"] },
  { id: "gtts", label: "Google TTS", icon: Languages, prefix: "tts.gtts_prefix", fallback: ".", fields: ["tts.language"] },
];

function withDefaultOption(field: DashboardFieldDefinition, draft: Record<string, unknown>): DashboardFieldDefinition {
  if (field.id !== "tts.language") return field;
  const label = "Português — padrão do bot";
  const options = [{ value: "", label }, ...(field.options || []).filter(option => option.value !== "")];
  const current = String(draft[field.id] || "");
  if (field.id === "tts.language" && current && !options.some(option => option.value === current)) {
    const normalized = current.replace(/_/g, "-").toLowerCase();
    const language = options.find(option => option.value.toLowerCase() === normalized)
      || options.find(option => option.value === normalized.split("-")[0]);
    if (language) options.push({ value: current, label: `${language.label} — configuração atual` });
  }
  return { ...field, options };
}

export function TtsVoiceSettings({ fields, draft, ...common }: Props) {
  const engineFields = new Set(engines.flatMap(engine => [...engine.fields, engine.prefix]));
  const otherPrefixes = fields.filter(field => ["tts.atts_prefix", "tts.teto_prefix"].includes(field.id));
  for (const field of otherPrefixes) engineFields.add(field.id);
  const shared = fields.filter(field => field.id !== "tts.engine" && !engineFields.has(field.id));
  return <div className="osk-tts-settings">
    <div className="osk-tts-engines">{engines.map(engine => {
      const Icon = engine.icon;
      const controls = engine.fields.filter(id => id !== "tts.voice").map(id => fields.find(field => field.id === id)).filter((field): field is DashboardFieldDefinition => Boolean(field)).map(field => withDefaultOption(field, draft));
      const prefix = fields.find(field => field.id === engine.prefix);
      const voice = engine.id === "edge" ? fields.find(field => field.id === "tts.voice") : undefined;
      return <section key={engine.id} className="osk-settings-card osk-tts-engine" data-engine={engine.id}>
        <header><span className="osk-tts-engine-title"><Icon size={21} aria-hidden="true" /><h2>{engine.label}</h2></span>
          {prefix && <div className="osk-tts-prefix" data-changed={draft[prefix.id] !== common.values[prefix.id] || undefined}><span>Prefixo</span><DashboardFieldControl field={prefix} value={draft[prefix.id] ?? engine.fallback} guildOptions={common.guildOptions} onChange={common.onChange} /></div>}
        </header>
        {voice && <EdgeVoicePicker field={voice} value={draft[voice.id]} changed={draft[voice.id] !== common.values[voice.id]} guildOptions={common.guildOptions} onChange={common.onChange} />}
        {controls.length > 0 && <FieldsPanel {...common} sectionId="tts" fields={controls} draft={draft} />}
      </section>;
    })}</div>
    {otherPrefixes.length > 0 && <section className="osk-settings-card osk-tts-extra-prefixes"><header><Type size={19} aria-hidden="true" /><h2>Outros prefixos</h2></header><FieldsPanel {...common} sectionId="tts" fields={otherPrefixes} draft={draft} /></section>}
    {shared.length > 0 && <section className="osk-settings-card"><header><Mic2 size={19} aria-hidden="true" /><h2>Canal de voz</h2></header><FieldsPanel {...common} sectionId="tts" fields={shared} draft={draft} /></section>}
  </div>;
}
