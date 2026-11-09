import { AudioLines, Check, Disc3, Mic2, Music2, RotateCcw } from "lucide-react";
import { useId, useState } from "react";
import type { DashboardFieldDefinition, DashboardFieldOption, DashboardOptionsPayload } from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { SmartSelect } from "../SmartSelect";
import { sectionEditorValuesEqual } from "../sectionEditorModel";
import { useFieldFeedback } from "./FieldFeedback";

interface TtvSettingsProps {
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  onChange(field: DashboardFieldDefinition, value: unknown): void;
}

const VOICE_ART: Record<string, string> = {
  "kasane-teto": "/assets/ttv/kasane-teto.png",
  "hatsune-miku": "/assets/ttv/hatsune-miku.png",
  "anna-nyui": "/assets/ttv/anna-nyui.png",
  "pururu-purpura": "/assets/ttv/pururu-purpura.png",
};

function VoicePortrait({ voiceId }: { voiceId: string }) {
  const [failed, setFailed] = useState(false);
  const src = VOICE_ART[voiceId];
  return <span className="osk-ttv-voice-art" aria-hidden="true">
    {src && !failed
      ? <img src={src} alt="" width={160} height={160} loading="lazy" decoding="async" draggable={false} onError={() => setFailed(true)} />
      : <AudioLines size={36} />}
  </span>;
}

function languageName(language: string, voice?: DashboardFieldOption) {
  if (language === "pt-BR") return voice?.nativePortuguese ? "Português brasileiro" : "Português adaptado";
  if (language === "en-US") return "Inglês";
  return language;
}
function voiceLanguages(voice: DashboardFieldOption) {
  return (voice.languages || []).map(language => language === "pt-BR" ? "Português" : language === "en-US" ? "Inglês" : language).join(" · ");
}
function percentValue(value: unknown): number | string {
  if (value === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? Number((number * 100).toPrecision(14)) : String(value);
}
function toneText(value: string) {
  const pitch = Number(value);
  if (!value.trim() || !Number.isFinite(pitch)) return value || "—";
  if (pitch === 0) return "Original";
  return `${pitch > 0 ? "+" : ""}${String(pitch).replace(".", ",")} semitons`;
}

export function TtvSettings({ fields, values, draft, guildOptions, onChange }: TtvSettingsProps) {
  const uid = useId();
  const [notice, setNotice] = useState("");
  const { errors } = useFieldFeedback();
  const prefixField = fields.find(field => field.id === "tts.teto_prefix");
  const voiceField = fields.find(field => field.id === "tts.ttv_voice_id");
  const languageField = fields.find(field => field.id === "tts.ttv_language");
  const pitchField = fields.find(field => field.id === "tts.ttv_pitch_semitones");
  const rateField = fields.find(field => field.id === "tts.ttv_speech_rate");
  const prefix = String(draft["tts.teto_prefix"] ?? prefixField?.placeholder ?? "'");
  const botPrefix = String(draft["general.bot_prefix"] ?? values["general.bot_prefix"] ?? "_");
  const voices = voiceField?.options || [];
  const voiceId = String(draft["tts.ttv_voice_id"] ?? "pururu-purpura");
  const selectedVoice = voices.find(voice => voice.value === voiceId);
  const language = String(draft["tts.ttv_language"] ?? "pt-BR");
  const languages = (selectedVoice?.languages || languageField?.options?.map(option => option.value) || []).map(value => ({ value, label: languageName(value, selectedVoice) }));
  if (!languages.some(option => option.value === language)) languages.unshift({ value: language, label: `${languageName(language) || "Idioma salvo"} — configuração atual` });
  const pitchText = String(draft["tts.ttv_pitch_semitones"] ?? "+0.0");
  const pitch = Number(pitchText);
  const speed = draft["tts.ttv_speech_rate"] ?? 1;
  const rate = percentValue(speed);
  const changed = (field: DashboardFieldDefinition) => !sectionEditorValuesEqual(values[field.id], draft[field.id]);
  const errorId = (field: DashboardFieldDefinition) => `${uid}-${field.id}-error`;
  const feedback = (field: DashboardFieldDefinition) => errors[field.id] && <p className="osk-ttv-field-error" role="alert" id={errorId(field)}>{errors[field.id]}</p>;
  const changeVoice = (voice: DashboardFieldOption) => {
    if (!voiceField) return;
    onChange(voiceField, voice.value);
    if (languageField && voice.languages?.length && !voice.languages.includes(language)) {
      onChange(languageField, voice.languages[0]);
      setNotice(`${voice.label} usa português brasileiro. O idioma foi ajustado.`);
    } else setNotice("");
  };
  return <section className="osk-settings-card osk-ttv-settings" aria-label="TextToVocaloid">
    <header className="osk-ttv-heading"><span className="osk-ttv-emblem" aria-hidden="true"><AudioLines size={22} /></span><span><h2>TextToVocaloid</h2><p>Fala e canto com quatro vozes.</p></span></header>
    {prefixField && <div className="osk-ttv-server-prefix" data-changed={changed(prefixField) || undefined}>
      <div><label htmlFor={`field-${prefixField.id}`}>Prefixo</label><p>Use antes da frase no chat.</p></div>
      <DashboardFieldControl field={prefixField} value={draft[prefixField.id] ?? "'"} guildOptions={guildOptions} onChange={onChange} />
    </div>}
    {voiceField && <div className="osk-ttv-server-defaults">
      <header><h3>Voz padrão do servidor</h3><p>Quem já escolheu uma voz continua com a preferência pessoal.</p></header>
      <div data-field-id={voiceField.id} data-changed={changed(voiceField) || undefined} data-invalid={Boolean(errors[voiceField.id]) || undefined}>
        <div className="osk-ttv-voices" role="radiogroup" aria-label="Voz do TextToVocaloid" aria-invalid={Boolean(errors[voiceField.id]) || undefined} aria-describedby={errors[voiceField.id] ? errorId(voiceField) : undefined}>
          {voices.map((voice, index) => {
            const selected = voice.value === voiceId;
            return <label key={voice.value} className="osk-ttv-voice" data-voice-id={voice.value} data-selected={selected || undefined}>
              <input id={index === 0 ? `field-${voiceField.id}` : undefined} type="radio" name={`${uid}-voice`} value={voice.value} aria-label={voice.label} checked={selected} onChange={() => changeVoice(voice)} />
              <VoicePortrait voiceId={voice.value} />
              <strong>{voice.label}</strong><small>{voiceLanguages(voice)}</small><span className="osk-ttv-voice-check" aria-hidden="true">{selected && <Check size={13} />}</span>
            </label>;
          })}
        </div>
        {!selectedVoice && <p className="osk-ttv-notice" role="status">A voz salva não está no catálogo. Escolha outra para atualizar.</p>}
        {feedback(voiceField)}
      </div>
      <div className="osk-ttv-controls">
        {languageField && <div className="osk-ttv-language" data-field-id={languageField.id} data-changed={changed(languageField) || undefined} data-invalid={Boolean(errors[languageField.id]) || undefined}>
          <label htmlFor={`field-${languageField.id}`}>Idioma</label><SmartSelect id={`field-${languageField.id}`} ariaLabel="Idioma do TextToVocaloid" presentation="anchored" value={language} options={languages} invalid={Boolean(errors[languageField.id])} ariaDescribedBy={errors[languageField.id] ? errorId(languageField) : undefined} onChange={value => { onChange(languageField, value); setNotice(""); }} />
          {notice && <p className="osk-ttv-notice" role="status">{notice}</p>}{feedback(languageField)}
        </div>}
        {pitchField && <div className="osk-ttv-adjustment" data-field-id={pitchField.id} data-changed={changed(pitchField) || undefined} data-invalid={Boolean(errors[pitchField.id]) || undefined}>
          <header><label htmlFor={`field-${pitchField.id}`}>Tom</label><button type="button" className="osk-ttv-reset" disabled={pitchText.trim() !== "" && Number.isFinite(pitch) && pitch === 0} onClick={() => onChange(pitchField, "+0.0")} aria-label="Restaurar tom do TextToVocaloid padrão"><RotateCcw size={13} aria-hidden="true" />Padrão</button></header>
          <div className="osk-ttv-range"><input id={`field-${pitchField.id}`} type="range" min={-4} max={4} step={0.5} value={Number.isFinite(pitch) ? pitch : 0} aria-label="Tom do TextToVocaloid" aria-invalid={Boolean(errors[pitchField.id]) || undefined} aria-describedby={errors[pitchField.id] ? errorId(pitchField) : undefined} onChange={event => { const value = Number(event.target.value); onChange(pitchField, `${value >= 0 ? "+" : ""}${value.toFixed(1)}`); }} /><output htmlFor={`field-${pitchField.id}`}>{toneText(pitchText)}</output></div>{feedback(pitchField)}
        </div>}
        {rateField && <div className="osk-ttv-adjustment" data-field-id={rateField.id} data-changed={changed(rateField) || undefined} data-invalid={Boolean(errors[rateField.id]) || undefined}>
          <header><label htmlFor={`field-${rateField.id}`}>Velocidade da fala</label><button type="button" className="osk-ttv-reset" disabled={speed !== "" && Number(speed) === 1} onClick={() => onChange(rateField, 1)} aria-label="Restaurar velocidade do TextToVocaloid padrão"><RotateCcw size={13} aria-hidden="true" />Padrão</button></header>
          <div className="osk-ttv-range"><input type="range" min={50} max={150} step="any" value={typeof rate === "number" ? rate : 100} aria-label="Velocidade da fala" onChange={event => onChange(rateField, Math.round(Number(event.target.value)) / 100)} /><span className="osk-ttv-percent"><input id={`field-${rateField.id}`} type="number" min={50} max={150} step="any" value={rate} aria-label="Velocidade da fala (%)" aria-invalid={Boolean(errors[rateField.id]) || undefined} aria-describedby={errors[rateField.id] ? errorId(rateField) : undefined} onChange={event => onChange(rateField, event.target.value === "" ? "" : Number(event.target.value) / 100)} /><span aria-hidden="true">%</span></span></div>{feedback(rateField)}
        </div>}
      </div>
    </div>}
    <div className="osk-ttv-guide" aria-label="Como usar o TextToVocaloid">
      <div className="osk-ttv-speech-examples"><div><span><Mic2 size={15} aria-hidden="true" />Falar</span><code>{prefix}bom dia</code></div><div><span><Music2 size={15} aria-hidden="true" />Cantar</span><code>{prefix}bom dia~</code></div></div>
      <p>Termine a frase com <code>~</code> para cantar.</p>
      <div className="osk-ttv-cd-guide"><Disc3 size={18} aria-hidden="true" /><div><p><code>{botPrefix}cd</code><span>Escolha um CD no Discord.</span></p><p><code>{botPrefix}rcd</code><span>Ative ou desative CDs aleatórios para você.</span></p></div></div>
    </div>
  </section>;
}
