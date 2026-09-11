import type { DashboardSectionDefinition } from "../dashboardTypes.js";
import {
  TTS_ENGINE_OPTIONS,
  TTS_LANGUAGE_OPTIONS,
  TTS_VOICE_OPTIONS,
  BUTTON_STYLE_OPTIONS,
  TICKET_FLOW_OPTIONS,
  WELCOME_MODE_OPTIONS,
  WELCOME_STYLE_OPTIONS,
  WELCOME_COLOR_MODE_OPTIONS,
  WELCOME_MEDIA_MODE_OPTIONS,
  WELCOME_WEBHOOK_NAME_OPTIONS,
  WELCOME_WEBHOOK_AVATAR_OPTIONS,
  WELCOME_EMBED_IMAGE_MODE_OPTIONS,
  WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS,
  BIRTHDAY_LEAP_MODE_OPTIONS,
  WELCOME_TEMPLATE_VARIABLES,
  FORM_TEMPLATE_VARIABLES,
  BIRTHDAY_REGISTER_VARIABLES,
  BIRTHDAY_ANNOUNCE_VARIABLES,
  BIRTHDAY_CALENDAR_VARIABLES,
  messageField,
  ticketOptionFields
} from "../dashboardCatalogShared.js";

export const generalSection: DashboardSectionDefinition = {
  id: "general", label: "Geral", emoji: "⚙️", description: "Preferências aplicadas a todo o servidor.",
  fields: [
    { id: "general.bot_prefix", label: "Prefixo do bot", description: "Prefixo usado pelos comandos por mensagem.", type: "text", scope: "guild", path: "bot_prefix", maxLength: 8, placeholder: "_" },
    { id: "general.timezone", label: "Fuso horário", description: "Usado em aniversários e em futuras ações programadas do servidor.", type: "text", scope: "guild", path: "timezone", maxLength: 64, placeholder: "America/Sao_Paulo" },
  ],
};
