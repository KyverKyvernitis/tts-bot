import type { DashboardSectionDefinition } from "../dashboardTypes.js";
import { TTS_LANGUAGE_OPTIONS } from "../dashboardCatalogShared.js";
import { loadDashboardTtvCatalog } from "../../services/dashboardTtvModel.js";

const ttvCatalog = loadDashboardTtvCatalog();

export const ttsSection: DashboardSectionDefinition = {
  id: "tts", label: "Texto pra Voz", emoji: "🔊", description: "Vozes, idiomas, prefixos e comportamento de leitura.",
  groups: ["Ativação", "Voz", "Comportamento"],
  fields: [
    { id: "tts.enabled", label: "Texto pra Voz", description: "Ative para ler novas mensagens. Ao desativar, a fala atual termina e a fila pendente é descartada.", type: "boolean", scope: "guild", path: "tts_enabled", group: "Ativação" },
    { id: "tts.voice", label: "Voz", description: "Voz usada pelo Microsoft Edge.", type: "select", scope: "guild", path: "tts_defaults.voice", options: [{ value: "", label: "Padrão do bot" }], group: "Voz" },
    { id: "tts.language", label: "Idioma", description: "Idioma usado pelo Google TTS.", type: "select", scope: "guild", path: "tts_defaults.language", options: [{ value: "", label: "Português — padrão do bot" }, ...TTS_LANGUAGE_OPTIONS], group: "Voz" },
    { id: "tts.rate", label: "Velocidade", description: "Ajuste de -100% a +100%.", type: "text", scope: "guild", path: "tts_defaults.rate", maxLength: 16, placeholder: "+0%", group: "Voz" },
    { id: "tts.pitch", label: "Tom", description: "Ajuste de -100Hz a +100Hz.", type: "text", scope: "guild", path: "tts_defaults.pitch", maxLength: 16, placeholder: "+0Hz", group: "Voz" },
    { id: "tts.atts_prefix", label: "Prefixo ATTS", type: "text", scope: "guild", path: "atts_prefix", maxLength: 8, placeholder: "%", group: "Voz" },
    { id: "tts.teto_prefix", label: "Prefixo TextToVocaloid", description: "Prefixo usado para falar e cantar com as vozes do TextToVocaloid.", type: "text", scope: "guild", path: "teto_prefix", maxLength: 8, placeholder: "'", group: "Voz" },
    { id: "tts.ttv_voice_id", label: "Voz do servidor", description: "Padrão para quem não escolheu uma voz pessoal.", type: "select", scope: "guild", path: "tts_defaults.ttv_voice_id", options: ttvCatalog.voices.map(voice => ({ value: voice.id, label: voice.name, languages: voice.languages, nativePortuguese: voice.nativePortuguese })), group: "Voz" },
    { id: "tts.ttv_language", label: "Idioma", description: "Idioma padrão do TextToVocaloid neste servidor.", type: "select", scope: "guild", path: "tts_defaults.ttv_language", options: [{ value: "pt-BR", label: "Português (Brasil)" }, { value: "en-US", label: "Inglês (EUA)" }], group: "Voz" },
    { id: "tts.ttv_pitch_semitones", label: "Tom", description: "De −4 a +4 semitons, em passos de 0,5.", type: "text", scope: "guild", path: "tts_defaults.ttv_pitch_semitones", maxLength: 16, placeholder: "+0.0", group: "Voz" },
    { id: "tts.ttv_speech_rate", label: "Velocidade", description: "De 50% a 150% da velocidade original.", type: "number", scope: "guild", path: "tts_defaults.ttv_speech_rate", min: 0.5, max: 1.5, group: "Voz" },
    { id: "tts.gtts_prefix", label: "Prefixo gTTS", type: "text", scope: "guild", path: "gtts_prefix", maxLength: 8, placeholder: ".", group: "Voz" },
    { id: "tts.edge_prefix", label: "Prefixo Edge", type: "text", scope: "guild", path: "edge_prefix", maxLength: 8, placeholder: ",", group: "Voz" },
    { id: "tts.speech_limit_seconds", label: "Limite por leitura", description: "Tempo máximo reproduzido em cada mensagem.", type: "number", scope: "guild", path: "speech_limit_seconds", min: 1, max: 600, group: "Comportamento" },
    { id: "tts.announce_author_enabled", label: "Anunciar o nome do autor", description: "Lê o nome antes do conteúdo da mensagem.", type: "boolean", scope: "guild", path: "announce_author_enabled", group: "Comportamento" },
    { id: "tts.ignored_tts_role_id", label: "Cargo ignorado pelo TTS", type: "role", scope: "guild", path: "ignored_tts_role_id", group: "Comportamento" },
    { id: "tts.ignored_tts_role_enabled", label: "Ativar cargo ignorado", type: "boolean", scope: "guild", path: "ignored_tts_role_enabled", group: "Comportamento" },
  ],
};
