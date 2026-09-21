import type { DashboardSectionDefinition } from "../dashboardTypes.js";
import { TTS_LANGUAGE_OPTIONS, TTS_VOICE_OPTIONS } from "../dashboardCatalogShared.js";

export const ttsSection: DashboardSectionDefinition = {
  id: "tts", label: "Texto pra Voz", emoji: "🔊", description: "Vozes, idiomas, prefixos e comportamento de leitura.",
  groups: ["Ativação", "Voz", "Prefixos", "Comportamento"],
  fields: [
    { id: "tts.enabled", label: "Texto pra Voz", description: "Ative para ler novas mensagens. Ao desativar, a fala atual termina e a fila pendente é descartada.", type: "boolean", scope: "guild", path: "tts_enabled", group: "Ativação" },
    { id: "tts.voice", label: "Voz", description: "Voz usada pelo Microsoft Edge.", type: "select", scope: "guild", path: "tts_defaults.voice", options: [{ value: "", label: "Francisca — padrão do bot" }, ...TTS_VOICE_OPTIONS], group: "Voz" },
    { id: "tts.language", label: "Idioma", description: "Idioma usado pelo Google TTS.", type: "select", scope: "guild", path: "tts_defaults.language", options: [{ value: "", label: "Português — padrão do bot" }, ...TTS_LANGUAGE_OPTIONS], group: "Voz" },
    { id: "tts.rate", label: "Velocidade", description: "Ajuste de -100% a +100%.", type: "text", scope: "guild", path: "tts_defaults.rate", maxLength: 16, placeholder: "+0%", group: "Voz" },
    { id: "tts.pitch", label: "Tom", description: "Ajuste de -100Hz a +100Hz.", type: "text", scope: "guild", path: "tts_defaults.pitch", maxLength: 16, placeholder: "+0Hz", group: "Voz" },
    { id: "tts.voice_channel_id", label: "Canal de voz preferido", description: "Canal usado como referência quando configurado.", type: "channel", scope: "guild", path: "tts_voice_channel_id", group: "Voz" },
    { id: "tts.atts_prefix", label: "Prefixo ATTS", type: "text", scope: "guild", path: "atts_prefix", maxLength: 8, placeholder: "%", group: "Prefixos" },
    { id: "tts.teto_prefix", label: "Prefixo Kasane Teto", description: "Aciona a engine experimental no phone worker.", type: "text", scope: "guild", path: "teto_prefix", maxLength: 8, placeholder: "'", group: "Prefixos" },
    { id: "tts.gtts_prefix", label: "Prefixo gTTS", type: "text", scope: "guild", path: "gtts_prefix", maxLength: 8, placeholder: ".", group: "Prefixos" },
    { id: "tts.edge_prefix", label: "Prefixo Edge", type: "text", scope: "guild", path: "edge_prefix", maxLength: 8, placeholder: ",", group: "Prefixos" },
    { id: "tts.speech_limit_seconds", label: "Limite por leitura", description: "Tempo máximo reproduzido em cada mensagem.", type: "number", scope: "guild", path: "speech_limit_seconds", min: 1, max: 600, group: "Comportamento" },
    { id: "tts.announce_author_enabled", label: "Anunciar o nome do autor", description: "Lê o nome antes do conteúdo da mensagem.", type: "boolean", scope: "guild", path: "announce_author_enabled", group: "Comportamento" },
    { id: "tts.auto_leave_enabled", label: "Sair automaticamente", description: "Desconecta quando não houver mais ninguém no canal.", type: "boolean", scope: "guild", path: "auto_leave_enabled", group: "Comportamento" },
    { id: "tts.ignored_tts_role_id", label: "Cargo ignorado pelo TTS", type: "role", scope: "guild", path: "ignored_tts_role_id", group: "Comportamento" },
    { id: "tts.ignored_tts_role_enabled", label: "Ativar cargo ignorado", type: "boolean", scope: "guild", path: "ignored_tts_role_enabled", group: "Comportamento" },
  ],
};
