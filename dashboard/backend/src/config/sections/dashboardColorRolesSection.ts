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

export const colorRolesSection: DashboardSectionDefinition = {
  id: "color_roles", label: "Cargos de cor", emoji: "🎨", description: "Até três painéis com opções vinculadas aos cargos do servidor.",
  groups: ["Ativação", "Painel", "Mensagens"],
  groupMetadata: {
    Painel: {
      kind: "message",
      settingsFieldIds: ["color_roles.channel_id"],
      editors: [],
    },
    Mensagens: {
      kind: "message",
      editors: [{
        id: "color-role-feedback",
        label: "Mensagens de resultado",
        description: "Respostas exibidas ao aplicar, remover ou trocar uma cor.",
        fieldIds: [
          "color_roles.templates.apply", "color_roles.templates.remove", "color_roles.templates.switch",
          "color_roles.templates.no_role", "color_roles.templates.hierarchy", "color_roles.templates.missing_panel",
        ],
      }],
    },
  },
  fields: [
    { id: "color_roles.enabled", label: "Cargos de cor", description: "Ative para permitir novas escolhas. Os cargos já aplicados não serão removidos ao desativar.", type: "boolean", scope: "guild", path: "color_roles.enabled", group: "Ativação" },
    { id: "color_roles.channel_id", label: "Canal do painel", type: "channel", scope: "guild", path: "color_roles.channel_id", group: "Painel" },
    { id: "color_roles.panel_layout", label: "Painéis", description: "Adicione, remova e reorganize até três painéis.", type: "color_panel_layout", scope: "guild", path: "color_roles.panel_layout", group: "Painel" },
    { id: "color_roles.slots", label: "Opções dos painéis", description: "Nome e cargo vinculado de cada opção.", type: "color_slots", scope: "guild", path: "color_roles.slots", group: "Painel" },
    ...Object.entries({
      apply: "Cor aplicada", remove: "Cor removida", switch: "Cor trocada", no_role: "Cor sem cargo", hierarchy: "Erro de hierarquia", missing_panel: "Painel antigo",
    }).map(([key, label]) => ({ id: `color_roles.templates.${key}`, label, type: "textarea" as const, scope: "guild" as const, path: `color_roles.templates.${key}`, maxLength: 1000, group: "Mensagens" })),
  ],
};
