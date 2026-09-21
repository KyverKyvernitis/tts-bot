import type {
  DashboardChannelOption,
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardFormField,
  DashboardRoleOption,
} from "../types/dashboard";

export interface DashboardSelectOption {
  value: string;
  label: string;
  hint?: string;
  disabled?: boolean;
}

const TEXT_LIKE_CHANNEL_TYPES = new Set([0, 5, 15, 16]);
const VOICE_CHANNEL_TYPES = new Set([2, 13]);
const CATEGORY_CHANNEL_TYPE = 4;

const FUNCTION_TOGGLE_IDS = new Set([
  "welcome.enabled",
  "forms.enabled",
  "tickets.feature_enabled",
  "color_roles.enabled",
  "birthday.enabled",
  "tts.enabled",
]);

export function dashboardBooleanStateLabel(fieldId: string, value: unknown): string {
  if (FUNCTION_TOGGLE_IDS.has(fieldId)) return Boolean(value) ? "Ativa" : "Desativada";
  return Boolean(value) ? "Ativado" : "Desativado";
}

export function channelKindForField(field: DashboardFieldDefinition): "category" | "voice" | "text" {
  const hint = `${field.id} ${field.path}`.toLowerCase();
  if (hint.includes("category")) return "category";
  if (hint.includes("voice")) return "voice";
  return "text";
}

export function channelOptionsForField(
  field: DashboardFieldDefinition,
  channels: DashboardChannelOption[],
): DashboardSelectOption[] {
  const kind = channelKindForField(field);
  return channels.filter((channel) => {
    if (field.id === "economy.channel_id") return [0, 5, 2, 13].includes(channel.type);
    if (kind === "category") return channel.type === CATEGORY_CHANNEL_TYPE;
    if (kind === "voice") return VOICE_CHANNEL_TYPES.has(channel.type);
    return TEXT_LIKE_CHANNEL_TYPES.has(channel.type);
  }).map((channel) => {
    const permissionKnown = channel.permissionsKnown === true;
    const isWebhookChannel = field.id.includes("webhook.channel_id");
    const permissionAllowed = isWebhookChannel
      ? channel.webhookManageable !== false
      : kind === "category"
      ? channel.manageable !== false
      : kind === "voice"
        ? channel.connectable !== false
        : channel.sendable !== false;
    const permissionHint = !permissionKnown || permissionAllowed
      ? null
      : isWebhookChannel
        ? "A Osaka não pode gerenciar webhooks neste canal"
        : kind === "category"
        ? "A Osaka não pode gerenciar canais nesta categoria"
        : kind === "voice"
          ? "A Osaka não pode visualizar ou conectar neste canal"
          : "A Osaka não pode visualizar ou enviar mensagens neste canal";
    const organizationHint = channel.parentId ? "Canal organizado em uma categoria" : null;
    return {
      value: channel.id,
      label: kind === "category" ? `📁 ${channel.name}` : kind === "voice" ? `🔊 ${channel.name}` : `# ${channel.name}`,
      hint: permissionHint ?? organizationHint ?? undefined,
      disabled: Boolean(permissionHint),
    };
  });
}

export function channelOptionsWithCurrentValue(
  field: DashboardFieldDefinition,
  currentValue: string,
  channels: DashboardChannelOption[],
): DashboardSelectOption[] {
  const available = channelOptionsForField(field, channels);
  if (!currentValue || available.some((option) => option.value === currentValue)) return available;
  const channel = channels.find((item) => item.id === currentValue);
  const kind = channelKindForField(field);
  const label = channel
    ? kind === "category" ? `📁 ${channel.name}` : kind === "voice" ? `🔊 ${channel.name}` : `# ${channel.name}`
    : `Canal ${currentValue}`;
  return [{
    value: currentValue,
    label,
    hint: "Canal atual indisponível para esta configuração",
    disabled: true,
  }, ...available];
}

export function roleOptionsWithCurrentValue(
  field: DashboardFieldDefinition,
  value: unknown,
  currentValue: string,
  roles: DashboardRoleOption[],
): DashboardSelectOption[] {
  const available = roles
    .filter((role) => field.id === "economy.staff_role_id" || (!role.managed && role.assignable !== false))
    .map((role) => ({
      value: role.id,
      label: `@${role.name}`,
      hint: role.color ? `Cor do cargo: #${role.color.toString(16).padStart(6, "0")}` : undefined,
    }));
  const currentIds = field.type === "role_multi" && Array.isArray(value)
    ? value.map(String)
    : currentValue ? [currentValue] : [];
  const unavailable = currentIds
    .filter((id) => !available.some((option) => option.value === id))
    .map((id) => {
      const role = roles.find((item) => item.id === id);
      return { value: id, label: role ? `@${role.name}` : `Cargo ${id}`, hint: "Cargo atual indisponível para atribuição" };
    });
  return [...unavailable, ...available];
}

export function normalizeDashboardFormField(value: unknown, index: number): DashboardFormField {
  const raw = value && typeof value === "object" ? value as Partial<DashboardFormField> : {};
  return {
    id: String(raw.id || `field${index + 1}`),
    label: String(raw.label || `Pergunta ${index + 1}`),
    placeholder: String(raw.placeholder || ""),
    response_label: String(raw.response_label || raw.label || `Pergunta ${index + 1}`),
    required: raw.required !== false,
    long: Boolean(raw.long),
    show_in_response: raw.show_in_response !== false,
    enabled: raw.enabled !== false,
    min_length: Number(raw.min_length || 0),
    max_length: Number(raw.max_length || (raw.long ? 1000 : 120)),
  };
}

export function stringifyDashboardValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  return String(value);
}

export function displayDashboardValue(
  field: DashboardFieldDefinition,
  value: unknown,
  guildOptions: DashboardOptionsPayload | null,
): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if (field.type === "role_multi") return Array.isArray(value) && value.length ? `${value.length} cargo${value.length === 1 ? "" : "s"}` : "Nenhum cargo";
  if (field.type === "string_list") return Array.isArray(value) && value.length ? `${value.length} item${value.length === 1 ? "" : "s"}` : "Lista vazia";
  if (field.type === "form_fields") return Array.isArray(value) && value.length ? `${value.length} pergunta${value.length === 1 ? "" : "s"}` : "Nenhuma pergunta";
  if (field.type === "color_slots") return value && typeof value === "object" ? `${Object.keys(value as object).length} opções` : "Nenhuma opção";
  if (field.type === "color_panel_layout") return Array.isArray(value) ? `${value.length} painel${value.length === 1 ? "" : "éis"}` : "Nenhum painel";
  if (field.type === "channel" || field.type === "role") {
    const id = stringifyDashboardValue(value);
    if (!id || Number(id) <= 0) return "Não configurado";
    const list = field.type === "channel" ? guildOptions?.channels : guildOptions?.roles;
    const match = list?.find((item) => item.id === id);
    if (match) return field.type === "channel" ? `#${match.name}` : `@${match.name}`;
    return id;
  }
  if (field.type === "select") {
    const raw = stringifyDashboardValue(value);
    return (field.options?.find((item) => item.value === raw)?.label ?? raw) || "Não configurado";
  }
  const text = stringifyDashboardValue(value).trim();
  return text || "Não configurado";
}
