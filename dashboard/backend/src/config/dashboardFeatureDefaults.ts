import { BIRTHDAY_DOC_CONFIG, WELCOME_DOC_CONFIG } from "./dashboardDocumentIds.js";
import { snowflakeToLong } from "./dashboardSnowflakes.js";

export function defaultWelcomeDoc(guildId: string): Record<string, unknown> {
  return {
    type: WELCOME_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    channel_id: 0,
    render_mode: "components_v2",
    public: { title: "Bem-vindo(a)!", body: "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.", footer: "Você é o membro #{contador}." },
    dm_enabled: false,
    delete_on_leave_enabled: false,
    decorative_emoji_enabled: false,
    auto_role_ids: [],
    style: "complete",
    dm_render_mode: "components_v2",
    accent_color: "#5865F2",
    accent_color_mode: "fixed",
    media_url: "",
    media_mode: "custom",
    embed: {
      content: "", author_name: "", author_icon_mode: "none", author_icon_url: "", author_url: "", title: "", title_url: "",
      description: "", color: "", color_mode: "fixed", thumbnail_mode: "none", thumbnail_url: "", image_mode: "custom", image_url: "",
      footer_text: "", footer_icon_mode: "none", footer_icon_url: "",
    },
    dm: { title: "Bem-vindo(a) ao {servidor}!", body: "Que bom ter você por aqui, {membro}. Aproveite o servidor.", footer: "" },
    webhook: { enabled: false, channel_id: 0, webhook_id: 0, webhook_token: "", name: "Boas-vindas", name_mode: "fixed", avatar_mode: "server", avatar_url: "" },
    variants: [], mode_configs: {}, invite_cache: {}, special_rules: [],
  };
}

export function defaultBirthdayDoc(guildId: string): Record<string, unknown> {
  return {
    type: BIRTHDAY_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    register_channel_id: 0,
    announce_channel_id: 0,
    timezone: "America/Sao_Paulo",
    announce_hour: 9,
    announce_minute: 0,
    options: { allow_update: true, show_age: true, group_announcements: true, delete_on_leave: true, leap_day_mode: "feb28", valid_reaction: "✅" },
    templates: {
      calendar: "# 🎂 Aniversários${birthdaycalendarblock}",
      saved: "Prontinho, ${usermention}. Seu aniversário foi salvo como **${birthdaydate}** 🎂",
      updated: "Prontinho, ${usermention}. Atualizei seu aniversário para **${birthdaydate}** 🎂",
      invalid: "Data inválida. Mande uma data no estilo **dia/mês**.",
      announce_single: "🎂 Feliz aniversário, ${usermention}! Hoje é seu dia.",
      announce_group: "🎂 Hoje temos ${birthdaycount} aniversariante(s)!\n\n${birthdaymentions}",
      empty_calendar: "Nenhum aniversário cadastrado ainda.",
    },
  };
}
