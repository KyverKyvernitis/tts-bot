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
  updateSettings(guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[] }>;
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
    description: "Canal, modo, mensagem pública e webhook de boas-vindas.",
    fields: [
      { id: "welcome.enabled", label: "Ativar boas-vindas", type: "boolean", scope: "welcome", path: "enabled" },
      { id: "welcome.channel_id", label: "Canal de boas-vindas", type: "channel", scope: "welcome", path: "channel_id" },
      { id: "welcome.render_mode", label: "Modo de envio", type: "select", scope: "welcome", path: "render_mode", options: WELCOME_MODE_OPTIONS },
      { id: "welcome.public.title", label: "Título", type: "text", scope: "welcome", path: "public.title", maxLength: 180, placeholder: "Bem-vindo(a)!" },
      { id: "welcome.public.body", label: "Mensagem", type: "textarea", scope: "welcome", path: "public.body", maxLength: 1800, placeholder: "Olá, {membro_mencao}. Seja bem-vindo(a) ao {servidor}." },
      { id: "welcome.public.footer", label: "Rodapé", type: "text", scope: "welcome", path: "public.footer", maxLength: 300 },
      { id: "welcome.webhook.enabled", label: "Usar webhook", type: "boolean", scope: "welcome", path: "webhook.enabled" },
      { id: "welcome.webhook.name", label: "Nome do webhook", type: "text", scope: "welcome", path: "webhook.name", maxLength: 80, placeholder: "Boas-vindas" },
      { id: "welcome.delete_on_leave_enabled", label: "Apagar se sair em até 24h", type: "boolean", scope: "welcome", path: "delete_on_leave_enabled" },
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
    description: "Canal de cadastro, canal de avisos, calendário e fuso.",
    fields: [
      { id: "birthday.enabled", label: "Ativar aniversários", type: "boolean", scope: "birthday", path: "enabled" },
      { id: "birthday.register_channel_id", label: "Canal/thread de cadastro", type: "channel", scope: "birthday", path: "register_channel_id" },
      { id: "birthday.announce_channel_id", label: "Canal de avisos", type: "channel", scope: "birthday", path: "announce_channel_id" },
      { id: "birthday.calendar_channel_id", label: "Canal do calendário", type: "channel", scope: "birthday", path: "calendar_channel_id" },
      { id: "birthday.timezone", label: "Fuso horário", type: "text", scope: "birthday", path: "timezone", maxLength: 64, placeholder: "America/Sao_Paulo" },
      { id: "birthday.announce_hour", label: "Hora do aviso", type: "number", scope: "birthday", path: "announce_hour", min: 0, max: 23 },
      { id: "birthday.announce_minute", label: "Minuto do aviso", type: "number", scope: "birthday", path: "announce_minute", min: 0, max: 59 },
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
  {
    id: "logs",
    label: "Logs",
    emoji: "📜",
    description: "Canais usados para auditoria e registros do servidor.",
    fields: [
      { id: "logs.admin_channel_id", label: "Logs admin", type: "channel", scope: "guild", path: "logs.admin_channel_id" },
      { id: "logs.error_channel_id", label: "Logs de erro", type: "channel", scope: "guild", path: "logs.error_channel_id" },
      { id: "logs.update_channel_id", label: "Logs de update", type: "channel", scope: "guild", path: "logs.update_channel_id" },
      { id: "logs.tts_channel_id", label: "Logs de TTS", type: "channel", scope: "guild", path: "logs.tts_channel_id" },
      { id: "logs.tickets_channel_id", label: "Logs de tickets", type: "channel", scope: "guild", path: "tickets.channels.logs_channel_id" },
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
    webhook: { enabled: false, name: "Boas-vindas" },
    delete_on_leave_enabled: false,
  };
}

function defaultBirthdayDoc(guildId: string): Record<string, unknown> {
  return {
    type: BIRTHDAY_DOC_CONFIG,
    guild_id: snowflakeToLong(guildId),
    enabled: false,
    register_channel_id: 0,
    announce_channel_id: 0,
    calendar_channel_id: 0,
    timezone: "America/Sao_Paulo",
    announce_hour: 9,
    announce_minute: 0,
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

  async function saveDocs(guildId: string, patches: Map<DashboardFieldScope, Record<string, unknown>>) {
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
      for (const [fieldId, rawValue] of Object.entries(updates || {})) {
        const field = fieldsById.get(fieldId);
        if (!field) continue;
        const value = normalizeFieldValue(field, rawValue);
        setPath(docs[field.scope], field.path, value);
        const scopePatch = patches.get(field.scope) ?? {};
        dotSetForPath(scopePatch, field.path, value);
        patches.set(field.scope, scopePatch);
        saved.push(field.id);
      }
      if (saved.length) {
        await saveDocs(guildId, patches);
      }
      return { ok: true, values: valuesFromDocs(docs), saved };
    },
  };
}
