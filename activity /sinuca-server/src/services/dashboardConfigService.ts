import { Long, MongoClient, type Collection, type Db, type Document } from "mongodb";

export type DashboardFieldType = "boolean" | "text" | "textarea" | "number" | "channel" | "role" | "select" | "color" | "url";
export type DashboardFieldScope = "guild" | "welcome" | "birthday";

export interface DashboardFieldOption {
  value: string;
  label: string;
}

export interface DashboardFieldDefinition {
  id: string;
  label: string;
  description?: string;
  type: DashboardFieldType;
  scope: DashboardFieldScope;
  path: string;
  placeholder?: string;
  min?: number;
  max?: number;
  maxLength?: number;
  options?: DashboardFieldOption[];
}

export interface DashboardSectionDefinition {
  id: string;
  label: string;
  emoji: string;
  description: string;
  fields: DashboardFieldDefinition[];
  actions?: Array<{ id: string; label: string; description?: string }>;
}

export interface DashboardGuildSummary {
  guildId: string;
  sections: Array<{
    id: string;
    label: string;
    emoji: string;
    description: string;
    enabled: boolean | null;
    configured: number;
    total: number;
    status: string;
  }>;
}

export interface DashboardConfigService {
  listSections(): DashboardSectionDefinition[];
  getSummary(guildId: string): Promise<DashboardGuildSummary>;
  getSettings(guildId: string): Promise<{ guildId: string; sections: DashboardSectionDefinition[]; values: Record<string, unknown> }>;
  updateSettings(guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[]; revision?: number; changed_sections?: string[] }>;
}

interface CreateDashboardConfigServiceOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName: string;
}

const WELCOME_DOC_CONFIG = "welcome_config";
const BIRTHDAY_DOC_CONFIG = "birthday_config";

const TTS_ENGINE_OPTIONS = [
  { value: "gtts", label: "gTTS" },
  { value: "edge", label: "Edge" },
  { value: "gcloud", label: "Google Cloud" },
  { value: "android_native", label: "Android nativo / ATTS" },
];

const WELCOME_MODE_OPTIONS = [
  { value: "components_v2", label: "Components V2" },
  { value: "embed", label: "Embed" },
  { value: "normal", label: "Mensagem normal" },
];

const WELCOME_STYLE_OPTIONS = [
  { value: "complete", label: "Completo" },
  { value: "simple", label: "Simples" },
  { value: "compact", label: "Compacto" },
];

const WELCOME_COLOR_MODE_OPTIONS = [
  { value: "fixed", label: "Cor fixa" },
  { value: "member_avatar", label: "Combinar com avatar do membro" },
];

const WELCOME_MEDIA_MODE_OPTIONS = [
  { value: "custom", label: "Link personalizado" },
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];

const WELCOME_WEBHOOK_NAME_OPTIONS = [
  { value: "fixed", label: "Nome personalizado" },
  { value: "server", label: "Nome do servidor" },
  { value: "member", label: "Nome do membro" },
  { value: "inviter", label: "Nome de quem convidou" },
];

const WELCOME_WEBHOOK_AVATAR_OPTIONS = [
  { value: "server", label: "Avatar do servidor" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "custom", label: "Avatar por link" },
];

const WELCOME_EMBED_IMAGE_MODE_OPTIONS = [
  { value: "none", label: "Sem imagem" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "server", label: "Ícone do servidor" },
  { value: "bot", label: "Avatar do bot" },
  { value: "custom", label: "Link personalizado" },
];

const WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS = [
  ...WELCOME_EMBED_IMAGE_MODE_OPTIONS,
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];

const BIRTHDAY_LEAP_MODE_OPTIONS = [
  { value: "feb28", label: "Avisar em 28/02" },
  { value: "mar01", label: "Avisar em 01/03" },
];

const sections: DashboardSectionDefinition[] = [
  {
    id: "general",
    label: "Geral",
    emoji: "⚙️",
    description: "Prefixo, identidade visual e preferências básicas do servidor.",
    fields: [
      { id: "general.bot_prefix", label: "Prefixo do bot", type: "text", scope: "guild", path: "bot_prefix", maxLength: 8, placeholder: "_" },
      { id: "general.dashboard_accent_color", label: "Cor padrão do dashboard", type: "color", scope: "guild", path: "dashboard_accent_color", placeholder: "#5865F2" },
      { id: "general.timezone", label: "Fuso horário", type: "text", scope: "guild", path: "timezone", maxLength: 64, placeholder: "America/Sao_Paulo" },
    ],
  },
  {
    id: "welcome",
    label: "Boas-vindas",
    emoji: "👋",
    description: "Canal, modo, aparência, mensagens, DM e webhook de boas-vindas.",
    fields: [
      { id: "welcome.enabled", label: "Ativar boas-vindas", type: "boolean", scope: "welcome", path: "enabled" },
      { id: "welcome.channel_id", label: "Canal de boas-vindas", type: "channel", scope: "welcome", path: "channel_id" },
      { id: "welcome.render_mode", label: "Modo público", type: "select", scope: "welcome", path: "render_mode", options: WELCOME_MODE_OPTIONS },
      { id: "welcome.style", label: "Estilo Components V2", type: "select", scope: "welcome", path: "style", options: WELCOME_STYLE_OPTIONS },
      { id: "welcome.delete_on_leave_enabled", label: "Apagar se sair em até 24h", type: "boolean", scope: "welcome", path: "delete_on_leave_enabled" },
      { id: "welcome.dm_enabled", label: "Enviar mensagem no privado", type: "boolean", scope: "welcome", path: "dm_enabled" },
      { id: "welcome.dm_render_mode", label: "Modo da DM", type: "select", scope: "welcome", path: "dm_render_mode", options: WELCOME_MODE_OPTIONS },
      { id: "welcome.decorative_emoji_enabled", label: "Emojis decorativos", type: "boolean", scope: "welcome", path: "decorative_emoji_enabled" },
      { id: "welcome.accent_color", label: "Cor de destaque", type: "color", scope: "welcome", path: "accent_color", placeholder: "#5865F2" },
      { id: "welcome.accent_color_mode", label: "Modo da cor", type: "select", scope: "welcome", path: "accent_color_mode", options: WELCOME_COLOR_MODE_OPTIONS },
      { id: "welcome.media_mode", label: "Imagem/banner", type: "select", scope: "welcome", path: "media_mode", options: WELCOME_MEDIA_MODE_OPTIONS },
      { id: "welcome.media_url", label: "URL da imagem/banner", type: "url", scope: "welcome", path: "media_url", maxLength: 1000, placeholder: "https://exemplo.com/banner.png" },

      { id: "welcome.public.title", label: "Título público", type: "text", scope: "welcome", path: "public.title", maxLength: 256, placeholder: "Bem-vindo(a)!" },
      { id: "welcome.public.body", label: "Mensagem pública", type: "textarea", scope: "welcome", path: "public.body", maxLength: 1800, placeholder: "Olá, {membro_mencao}. Seja bem-vindo(a) ao {servidor}." },
      { id: "welcome.public.footer", label: "Rodapé público", type: "text", scope: "welcome", path: "public.footer", maxLength: 300 },

      { id: "welcome.embed.content", label: "Texto acima do embed", type: "textarea", scope: "welcome", path: "embed.content", maxLength: 1800 },
      { id: "welcome.embed.author_name", label: "Embed: autor", type: "text", scope: "welcome", path: "embed.author_name", maxLength: 256 },
      { id: "welcome.embed.author_icon_mode", label: "Embed: ícone do autor", type: "select", scope: "welcome", path: "embed.author_icon_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS },
      { id: "welcome.embed.author_icon_url", label: "Embed: URL do ícone do autor", type: "url", scope: "welcome", path: "embed.author_icon_url", maxLength: 1000 },
      { id: "welcome.embed.author_url", label: "Embed: URL do autor", type: "url", scope: "welcome", path: "embed.author_url", maxLength: 1000 },
      { id: "welcome.embed.title", label: "Embed: título", type: "text", scope: "welcome", path: "embed.title", maxLength: 256 },
      { id: "welcome.embed.title_url", label: "Embed: URL do título", type: "url", scope: "welcome", path: "embed.title_url", maxLength: 1000 },
      { id: "welcome.embed.description", label: "Embed: descrição", type: "textarea", scope: "welcome", path: "embed.description", maxLength: 1800 },
      { id: "welcome.embed.color", label: "Embed: cor", type: "color", scope: "welcome", path: "embed.color", placeholder: "#5865F2" },
      { id: "welcome.embed.color_mode", label: "Embed: modo da cor", type: "select", scope: "welcome", path: "embed.color_mode", options: WELCOME_COLOR_MODE_OPTIONS },
      { id: "welcome.embed.thumbnail_mode", label: "Embed: thumbnail", type: "select", scope: "welcome", path: "embed.thumbnail_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS },
      { id: "welcome.embed.thumbnail_url", label: "Embed: URL da thumbnail", type: "url", scope: "welcome", path: "embed.thumbnail_url", maxLength: 1000 },
      { id: "welcome.embed.image_mode", label: "Embed: imagem principal", type: "select", scope: "welcome", path: "embed.image_mode", options: WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS },
      { id: "welcome.embed.image_url", label: "Embed: URL da imagem", type: "url", scope: "welcome", path: "embed.image_url", maxLength: 1000 },
      { id: "welcome.embed.footer_text", label: "Embed: rodapé", type: "text", scope: "welcome", path: "embed.footer_text", maxLength: 2048 },
      { id: "welcome.embed.footer_icon_mode", label: "Embed: ícone do rodapé", type: "select", scope: "welcome", path: "embed.footer_icon_mode", options: WELCOME_EMBED_IMAGE_MODE_OPTIONS },
      { id: "welcome.embed.footer_icon_url", label: "Embed: URL do ícone do rodapé", type: "url", scope: "welcome", path: "embed.footer_icon_url", maxLength: 1000 },

      { id: "welcome.dm.title", label: "DM: título", type: "text", scope: "welcome", path: "dm.title", maxLength: 256, placeholder: "Bem-vindo(a) ao {servidor}!" },
      { id: "welcome.dm.body", label: "DM: mensagem", type: "textarea", scope: "welcome", path: "dm.body", maxLength: 1800 },
      { id: "welcome.dm.footer", label: "DM: rodapé", type: "text", scope: "welcome", path: "dm.footer", maxLength: 300 },

      { id: "welcome.webhook.enabled", label: "Usar webhook", type: "boolean", scope: "welcome", path: "webhook.enabled" },
      { id: "welcome.webhook.name_mode", label: "Nome do webhook", type: "select", scope: "welcome", path: "webhook.name_mode", options: WELCOME_WEBHOOK_NAME_OPTIONS },
      { id: "welcome.webhook.name", label: "Nome personalizado do webhook", type: "text", scope: "welcome", path: "webhook.name", maxLength: 80, placeholder: "Boas-vindas" },
      { id: "welcome.webhook.avatar_mode", label: "Avatar do webhook", type: "select", scope: "welcome", path: "webhook.avatar_mode", options: WELCOME_WEBHOOK_AVATAR_OPTIONS },
      { id: "welcome.webhook.avatar_url", label: "URL do avatar do webhook", type: "url", scope: "welcome", path: "webhook.avatar_url", maxLength: 1000 },
    ],
    actions: [
      { id: "preview_welcome", label: "Preview", description: "Mostra como a mensagem ficaria para um membro de teste." },
    ],
  },
  {
    id: "tickets",
    label: "Tickets",
    emoji: "🎫",
    description: "Painel de atendimento, categoria, logs, cargos staff e fluxos.",
    fields: [
      { id: "tickets.panel.channel_id", label: "Canal do painel", type: "channel", scope: "guild", path: "tickets.panel.channel_id" },
      { id: "tickets.channels.category_id", label: "Categoria dos tickets", type: "channel", scope: "guild", path: "tickets.channels.category_id" },
      { id: "tickets.channels.logs_channel_id", label: "Canal de logs", type: "channel", scope: "guild", path: "tickets.channels.logs_channel_id" },
      { id: "tickets.channels.suggestions_channel_id", label: "Canal de sugestões", type: "channel", scope: "guild", path: "tickets.channels.suggestions_channel_id" },
      { id: "tickets.roles.staff_role_id", label: "Cargo staff padrão", type: "role", scope: "guild", path: "tickets.roles.staff_role_id" },
      { id: "tickets.options.use_server_webhook", label: "Usar webhook do servidor", type: "boolean", scope: "guild", path: "tickets.options.use_server_webhook" },
      { id: "tickets.enabled.partnership", label: "Fluxo parceria", type: "boolean", scope: "guild", path: "tickets.enabled.partnership" },
      { id: "tickets.enabled.report", label: "Fluxo denúncia", type: "boolean", scope: "guild", path: "tickets.enabled.report" },
      { id: "tickets.enabled.suggestion", label: "Fluxo sugestão", type: "boolean", scope: "guild", path: "tickets.enabled.suggestion" },
      { id: "tickets.enabled.other", label: "Fluxo outros", type: "boolean", scope: "guild", path: "tickets.enabled.other" },
    ],
  },
  {
    id: "birthday",
    label: "Aniversários",
    emoji: "🎂",
    description: "Cadastro, avisos, calendário, mensagens e preferências de aniversário.",
    fields: [
      { id: "birthday.enabled", label: "Ativar aniversários", type: "boolean", scope: "birthday", path: "enabled" },
      { id: "birthday.register_channel_id", label: "Canal do calendário/cadastro", type: "channel", scope: "birthday", path: "register_channel_id", description: "Canal onde fica a mensagem pública e a thread de cadastro." },
      { id: "birthday.announce_channel_id", label: "Canal de avisos", type: "channel", scope: "birthday", path: "announce_channel_id" },
      { id: "birthday.timezone", label: "Fuso horário", type: "text", scope: "birthday", path: "timezone", maxLength: 64, placeholder: "America/Sao_Paulo" },
      { id: "birthday.announce_hour", label: "Hora do aviso", type: "number", scope: "birthday", path: "announce_hour", min: 0, max: 23 },
      { id: "birthday.announce_minute", label: "Minuto do aviso", type: "number", scope: "birthday", path: "announce_minute", min: 0, max: 59 },
      { id: "birthday.options.show_age", label: "Mostrar idade nos avisos", type: "boolean", scope: "birthday", path: "options.show_age" },
      { id: "birthday.options.group_announcements", label: "Agrupar aniversariantes do dia", type: "boolean", scope: "birthday", path: "options.group_announcements" },
      { id: "birthday.options.delete_on_leave", label: "Remover quando sair do servidor", type: "boolean", scope: "birthday", path: "options.delete_on_leave" },
      { id: "birthday.options.leap_day_mode", label: "Aniversário em 29/02", type: "select", scope: "birthday", path: "options.leap_day_mode", options: BIRTHDAY_LEAP_MODE_OPTIONS },
      { id: "birthday.options.valid_reaction", label: "Reação em data válida", type: "text", scope: "birthday", path: "options.valid_reaction", maxLength: 20, placeholder: "✅" },
      { id: "birthday.templates.calendar", label: "Template do calendário", type: "textarea", scope: "birthday", path: "templates.calendar", maxLength: 1800 },
      { id: "birthday.templates.saved", label: "Mensagem ao salvar", type: "textarea", scope: "birthday", path: "templates.saved", maxLength: 1800 },
      { id: "birthday.templates.updated", label: "Mensagem ao atualizar", type: "textarea", scope: "birthday", path: "templates.updated", maxLength: 1800 },
      { id: "birthday.templates.invalid", label: "Mensagem de data inválida", type: "textarea", scope: "birthday", path: "templates.invalid", maxLength: 1800 },
      { id: "birthday.templates.announce_single", label: "Aviso individual", type: "textarea", scope: "birthday", path: "templates.announce_single", maxLength: 1800 },
      { id: "birthday.templates.announce_group", label: "Aviso agrupado", type: "textarea", scope: "birthday", path: "templates.announce_group", maxLength: 1800 },
      { id: "birthday.templates.empty_calendar", label: "Calendário vazio", type: "textarea", scope: "birthday", path: "templates.empty_calendar", maxLength: 1800 },
    ],
  },
  {
    id: "tts",
    label: "TTS",
    emoji: "🔊",
    description: "Engine, prefixos, canal de voz, limites e comportamento padrão.",
    fields: [
      { id: "tts.engine", label: "Engine padrão", type: "select", scope: "guild", path: "tts_defaults.engine", options: TTS_ENGINE_OPTIONS },
      { id: "tts.voice_channel_id", label: "Canal de voz padrão", type: "channel", scope: "guild", path: "tts_voice_channel_id" },
      { id: "tts.tts_prefix", label: "Prefixo TTS", type: "text", scope: "guild", path: "tts_prefix", maxLength: 8, placeholder: "," },
      { id: "tts.gtts_prefix", label: "Prefixo gTTS", type: "text", scope: "guild", path: "gtts_prefix", maxLength: 8, placeholder: "." },
      { id: "tts.edge_prefix", label: "Prefixo Edge", type: "text", scope: "guild", path: "edge_prefix", maxLength: 8, placeholder: "," },
      { id: "tts.gcloud_prefix", label: "Prefixo Google", type: "text", scope: "guild", path: "gcloud_prefix", maxLength: 8, placeholder: "'" },
      { id: "tts.atts_prefix", label: "Prefixo ATTS", type: "text", scope: "guild", path: "atts_prefix", maxLength: 8, placeholder: "%" },
      { id: "tts.speech_limit_seconds", label: "Limite por fala", type: "number", scope: "guild", path: "speech_limit_seconds", min: 1, max: 600 },
      { id: "tts.announce_author_enabled", label: "Anunciar autor", type: "boolean", scope: "guild", path: "announce_author_enabled" },
      { id: "tts.auto_leave_enabled", label: "Sair automaticamente", type: "boolean", scope: "guild", path: "auto_leave_enabled" },
      { id: "tts.ignored_tts_role_id", label: "Cargo ignorado pelo TTS", type: "role", scope: "guild", path: "ignored_tts_role_id" },
      { id: "tts.ignored_tts_role_enabled", label: "Ativar cargo ignorado", type: "boolean", scope: "guild", path: "ignored_tts_role_enabled" },
    ],
  },
  {
    id: "music",
    label: "Música",
    emoji: "🎵",
    description: "Preferências globais do player e permissões administrativas.",
    fields: [
      { id: "music.enabled", label: "Ativar música", type: "boolean", scope: "guild", path: "music.enabled" },
      { id: "music.channel_id", label: "Canal de música", type: "channel", scope: "guild", path: "music.channel_id" },
      { id: "music.dj_role_id", label: "Cargo DJ", type: "role", scope: "guild", path: "music.dj_role_id" },
      { id: "music.default_volume", label: "Volume padrão", type: "number", scope: "guild", path: "music.default_volume", min: 1, max: 200 },
    ],
  },
];


function snowflakeToLong(value: string): Long {
  const text = String(value ?? "").trim();
  if (!/^\d{1,25}$/.test(text)) {
    return Long.ZERO;
  }
  try {
    return Long.fromString(text, false);
  } catch {
    return Long.ZERO;
  }
}

function snowflakeFromRaw(raw: unknown): Long {
  const text = String(raw ?? "").trim();
  const match = text.match(/\d{15,25}/);
  if (!match) return Long.ZERO;
  return snowflakeToLong(match[0]);
}

function isLongLike(value: unknown): value is Long {
  return value instanceof Long || (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { toString?: unknown }).toString === "function" &&
    typeof (value as { low?: unknown }).low === "number" &&
    typeof (value as { high?: unknown }).high === "number"
  );
}

function serializeFieldValue(field: DashboardFieldDefinition, value: unknown): unknown {
  if (field.type === "channel" || field.type === "role") {
    if (isLongLike(value)) {
      const text = value.toString();
      return text === "0" ? "" : text;
    }
    if (typeof value === "number") {
      return Number.isFinite(value) && value > 0 ? String(Math.trunc(value)) : "";
    }
    const text = String(value ?? "").trim();
    return text === "0" ? "" : text;
  }
  return value;
}

function dotSetForPath(target: Record<string, unknown>, path: string, value: unknown) {
  const cleanPath = path.split(".").filter(Boolean).join(".");
  if (!cleanPath || cleanPath.includes("$") || cleanPath.includes("..")) {
    return;
  }
  target[cleanPath] = value;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value));
}

function getPath(source: Record<string, unknown>, path: string): unknown {
  let current: unknown = source;
  for (const part of path.split(".")) {
    if (!part) continue;
    if (typeof current !== "object" || current === null || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function setPath(target: Record<string, unknown>, path: string, value: unknown) {
  const parts = path.split(".").filter(Boolean);
  let current: Record<string, unknown> = target;
  for (const part of parts.slice(0, -1)) {
    const existing = current[part];
    if (typeof existing !== "object" || existing === null || Array.isArray(existing)) {
      current[part] = {};
    }
    current = current[part] as Record<string, unknown>;
  }
  current[parts[parts.length - 1]] = value;
}

function normalizeFieldValue(field: DashboardFieldDefinition, raw: unknown): unknown {
  if (field.type === "boolean") {
    return raw === true || raw === "true" || raw === "1" || raw === 1;
  }
  if (field.type === "number") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return field.min ?? 0;
    return Math.max(field.min ?? Number.MIN_SAFE_INTEGER, Math.min(field.max ?? Number.MAX_SAFE_INTEGER, Math.trunc(n)));
  }
  if (field.type === "channel" || field.type === "role") {
    return snowflakeFromRaw(raw);
  }
  if (field.type === "select") {
    const value = String(raw ?? "").trim();
    const allowed = new Set((field.options ?? []).map((item) => item.value));
    return allowed.has(value) ? value : ((field.options ?? [])[0]?.value ?? "");
  }
  if (field.type === "color") {
    const value = String(raw ?? "").trim();
    if (!value) return "";
    const normalized = value.startsWith("#") ? value : `#${value}`;
    if (/^#[0-9a-fA-F]{6}$/.test(normalized)) return normalized.toUpperCase();
    return "";
  }
  if (field.type === "url") {
    const value = String(raw ?? "").trim();
    if (!value) return "";
    if (/^https?:\/\/\S+$/i.test(value)) return value.slice(0, field.maxLength ?? 600);
    return "";
  }
  const text = String(raw ?? "");
  return text.slice(0, field.maxLength ?? (field.type === "textarea" ? 1800 : 300));
}

function allFields() {
  return sections.flatMap((section) => section.fields);
}

function defaultGuildDoc(guildId: string): Record<string, unknown> {
  return { type: "guild", guild_id: snowflakeToLong(guildId) };
}

function defaultWelcomeDoc(guildId: string): Record<string, unknown> {
  return {
    type: WELCOME_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    channel_id: 0,
    render_mode: "components_v2",
    public: {
      title: "Bem-vindo(a)!",
      body: "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.",
      footer: "Você é o membro #{contador}.",
    },
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
      content: "",
      author_name: "",
      author_icon_mode: "none",
      author_icon_url: "",
      author_url: "",
      title: "",
      title_url: "",
      description: "",
      color: "",
      color_mode: "fixed",
      thumbnail_mode: "none",
      thumbnail_url: "",
      image_mode: "custom",
      image_url: "",
      footer_text: "",
      footer_icon_mode: "none",
      footer_icon_url: "",
    },
    dm: {
      title: "Bem-vindo(a) ao {servidor}!",
      body: "Que bom ter você por aqui, {membro}. Aproveite o servidor.",
      footer: "",
    },
    webhook: {
      enabled: false,
      channel_id: 0,
      webhook_id: 0,
      webhook_token: "",
      name: "Boas-vindas",
      name_mode: "fixed",
      avatar_mode: "server",
      avatar_url: "",
    },
    variants: [],
    mode_configs: {},
    invite_cache: {},
    special_rules: [],
  };
}

function defaultBirthdayDoc(guildId: string): Record<string, unknown> {
  return {
    type: BIRTHDAY_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    register_channel_id: 0,
    announce_channel_id: 0,
    timezone: "America/Sao_Paulo",
    announce_hour: 9,
    announce_minute: 0,
    options: {
      allow_update: true,
      show_age: true,
      group_announcements: true,
      delete_on_leave: true,
      leap_day_mode: "feb28",
      valid_reaction: "✅",
    },
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

function isConfiguredValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value > 0;
  if (typeof value === "string") return value.trim().length > 0;
  return true;
}

export function createDashboardConfigService(options: CreateDashboardConfigServiceOptions): DashboardConfigService {
  let client: MongoClient | null = null;
  let db: Db | null = null;
  let coll: Collection<Document> | null = null;

  async function getCollection(): Promise<Collection<Document>> {
    if (!options.mongoUri) {
      throw new Error("mongodb_not_configured");
    }
    if (coll) return coll;
    client = new MongoClient(options.mongoUri);
    await client.connect();
    db = client.db(options.mongoDbName);
    coll = db.collection(options.mongoCollectionName);
    return coll;
  }

  async function getGuildDoc(guildId: string): Promise<Record<string, unknown>> {
    const collection = await getCollection();
    const doc = await collection.findOne({ type: "guild", guild_id: snowflakeToLong(guildId) }, { projection: { _id: 0 } });
    return { ...defaultGuildDoc(guildId), ...(doc as Record<string, unknown> | null ?? {}) };
  }

  async function getWelcomeDoc(guildId: string): Promise<Record<string, unknown>> {
    const collection = await getCollection();
    const doc = await collection.findOne({ type: WELCOME_DOC_CONFIG, guild_id: snowflakeToLong(guildId) }, { projection: { _id: 0 } });
    return { ...defaultWelcomeDoc(guildId), ...(doc as Record<string, unknown> | null ?? {}) };
  }

  async function getBirthdayDoc(guildId: string): Promise<Record<string, unknown>> {
    const collection = await getCollection();
    const doc = await collection.findOne({ type: BIRTHDAY_DOC_CONFIG, guild_id: snowflakeToLong(guildId) }, { projection: { _id: 0 } });
    return { ...defaultBirthdayDoc(guildId), ...(doc as Record<string, unknown> | null ?? {}) };
  }

  async function readAll(guildId: string) {
    const [guild, welcome, birthday] = await Promise.all([getGuildDoc(guildId), getWelcomeDoc(guildId), getBirthdayDoc(guildId)]);
    return { guild, welcome, birthday };
  }

  function valuesFromDocs(docs: Awaited<ReturnType<typeof readAll>>) {
    const values: Record<string, unknown> = {};
    for (const field of allFields()) {
      values[field.id] = serializeFieldValue(field, getPath(docs[field.scope], field.path));
    }
    return values;
  }

  async function saveDocs(guildId: string, patches: Map<DashboardFieldScope, Record<string, unknown>>, changedSections: string[]) {
    const collection = await getCollection();
    const guildIdValue = snowflakeToLong(guildId);
    const jobs: Promise<unknown>[] = [];

    const guildPatch = patches.get("guild");
    if (guildPatch && Object.keys(guildPatch).length) {
      jobs.push(collection.updateOne(
        { type: "guild", guild_id: guildIdValue },
        { $set: { type: "guild", guild_id: guildIdValue, ...guildPatch } },
        { upsert: true },
      ));
    }

    const welcomePatch = patches.get("welcome");
    if (welcomePatch && Object.keys(welcomePatch).length) {
      jobs.push(collection.updateOne(
        { type: WELCOME_DOC_CONFIG, guild_id: guildIdValue },
        { $set: { type: WELCOME_DOC_CONFIG, guild_id: guildIdValue, ...welcomePatch } },
        { upsert: true },
      ));
    }

    const birthdayPatch = patches.get("birthday");
    if (birthdayPatch && Object.keys(birthdayPatch).length) {
      jobs.push(collection.updateOne(
        { type: BIRTHDAY_DOC_CONFIG, guild_id: guildIdValue },
        { $set: { type: BIRTHDAY_DOC_CONFIG, guild_id: guildIdValue, ...birthdayPatch } },
        { upsert: true },
      ));
    }

    await Promise.all(jobs);

    const revisionResult = await collection.findOneAndUpdate(
      { type: "guild", guild_id: guildIdValue },
      {
        $set: {
          type: "guild",
          guild_id: guildIdValue,
          dashboard_updated_at: Math.floor(Date.now() / 1000),
          dashboard_changed_sections: changedSections,
        },
        $inc: { dashboard_revision: 1 },
      },
      { upsert: true, returnDocument: "after", projection: { _id: 0, dashboard_revision: 1 } },
    );

    const revision = revisionResult?.dashboard_revision;
    return typeof revision === "number" ? revision : undefined;
  }

  return {
    listSections() {
      return clone(sections);
    },

    async getSummary(guildId: string) {
      const docs = await readAll(guildId);
      const values = valuesFromDocs(docs);
      return {
        guildId,
        sections: sections.map((section) => {
          const enabledField = section.fields.find((field) => field.id.endsWith(".enabled"));
          const enabled = enabledField ? Boolean(values[enabledField.id]) : null;
          const configured = section.fields.filter((field) => isConfiguredValue(values[field.id])).length;
          const total = section.fields.length;
          return {
            id: section.id,
            label: section.label,
            emoji: section.emoji,
            description: section.description,
            enabled,
            configured,
            total,
            status: enabled === false ? "desativado" : configured > 0 ? "configurado" : "pendente",
          };
        }),
      };
    },

    async getSettings(guildId: string) {
      const docs = await readAll(guildId);
      return { guildId, sections: clone(sections), values: valuesFromDocs(docs) };
    },

    async updateSettings(guildId: string, updates: Record<string, unknown>) {
      const docs = await readAll(guildId);
      const fieldsById = new Map(allFields().map((field) => [field.id, field]));
      const patches = new Map<DashboardFieldScope, Record<string, unknown>>();
      const saved: string[] = [];
      const changedSections = new Set<string>();
      for (const [fieldId, rawValue] of Object.entries(updates || {})) {
        const field = fieldsById.get(fieldId);
        if (!field) continue;
        const value = normalizeFieldValue(field, rawValue);
        setPath(docs[field.scope], field.path, value);
        const scopePatch = patches.get(field.scope) ?? {};
        dotSetForPath(scopePatch, field.path, value);
        patches.set(field.scope, scopePatch);
        saved.push(field.id);
        changedSections.add(field.id.split(".")[0] || field.scope);
      }
      const changedSectionsList = Array.from(changedSections);
      const revision = saved.length ? await saveDocs(guildId, patches, changedSectionsList) : undefined;
      return { ok: true, values: valuesFromDocs(docs), saved, revision, changed_sections: changedSectionsList };
    },
  };
}
