import { defaultColorPanelLayout, defaultColorSlots } from "./dashboardColorRoleDefaults.js";
import { defaultFormsConfig } from "./dashboardFormsDefaults.js";
import { snowflakeToLong } from "./dashboardSnowflakes.js";
import { defaultTicketsConfig } from "./dashboardTicketsDefaults.js";

export function defaultGuildDoc(guildId: string): Record<string, unknown> {
  return {
    type: "guild",
    guild_id: snowflakeToLong(guildId),
    bot_prefix: "_",
    timezone: "America/Sao_Paulo",
    gincana_channel_id: 0,
    gincana_input_mode: "triggers",
    gincana_staff_role_id: 0,
    tts_prefix: ".",
    atts_prefix: "%",
    teto_prefix: "'",
    gtts_prefix: ".",
    edge_prefix: ",",
    speech_limit_seconds: 30,
    announce_author_enabled: false,
    auto_leave_enabled: true,
    ignored_tts_role_id: 0,
    ignored_tts_role_enabled: false,
    tts_enabled: true,
    tts_voice_channel_id: 0,
    tts_defaults: { engine: "edge", voice: "", language: "pt-BR", rate: "+0%", pitch: "+0Hz" },
    forms: defaultFormsConfig(),
    tickets: defaultTicketsConfig(),
    color_roles: {
      enabled: false,
      channel_id: 0,
      message_ids: [],
      panel_count: 3,
      panel_layout: defaultColorPanelLayout(),
      messages: Object.fromEntries([1, 2, 3, 4, 5].map((number) => [String(number), { title: "", subtitle: "", footer: "" }])),
      templates: {
        apply: "cor {cor_adicionada} aplicada.", remove: "cor {cor_removida} removida.", switch: "cor alterada: {cor_removida} → {cor_adicionada}.",
        no_role: "Essa cor ainda não está configurada.", hierarchy: "não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
        missing_panel: "Esse painel de cores não é mais o oficial deste servidor.",
      },
      slots: defaultColorSlots(),
    },
  };
}
