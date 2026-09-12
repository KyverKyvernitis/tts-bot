import type { DashboardFieldDefinition, DashboardMessageEditorPresentation } from "../../types/dashboard";
import { readableFieldLabel } from "./messageEditorUtils";

export interface PreviewSenderInput {
  senderFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  botName: string;
  botAvatarUrl?: string | null;
  guildName?: string;
  guildAvatarUrl?: string | null;
}

export interface PreviewSender {
  enabled: boolean;
  name: string;
  avatar?: string | null;
  badge: "BOT" | "APP";
}

export interface PreviewRenderPlan {
  isDm: boolean;
  welcomeMode: string;
  adaptiveWelcome: boolean;
  renderKind: "color-panel" | "components-v2" | "embed" | "generic";
}

export function findField(fields: DashboardFieldDefinition[], suffixes: string[]): DashboardFieldDefinition | undefined {
  return fields.find((item) => suffixes.some((suffix) => item.id.endsWith(suffix)));
}

export function findExact(fields: DashboardFieldDefinition[], ...ids: string[]): DashboardFieldDefinition | undefined {
  return fields.find((field) => ids.includes(field.id));
}

export function fieldValue(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): unknown {
  return field ? draft[field.id] : undefined;
}

export function fieldString(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): string {
  const value = fieldValue(field, draft);
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

export function normalizedColor(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  const normalized = trimmed.startsWith("#") ? trimmed : `#${trimmed}`;
  return /^#[0-9a-f]{6}$/i.test(normalized) ? normalized : null;
}

export function previewColor(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): string | null {
  const colorField = fields.find((field) => field.type === "color");
  return normalizedColor(colorField ? draft[colorField.id] : null);
}

export function needsLightOutline(hex: string): boolean {
  const match = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!match) return false;
  const value = Number.parseInt(match[1], 16);
  const red = (value >> 16) & 0xff;
  const green = (value >> 8) & 0xff;
  const blue = value & 0xff;
  return (red * 0.299 + green * 0.587 + blue * 0.114) < 58;
}

export function optionLabel(field: DashboardFieldDefinition | undefined, draft: Record<string, unknown>): string | null {
  if (!field) return null;
  const value = fieldString(field, draft);
  if (!value || value === "none") return null;
  return field.options?.find((option) => option.value === value)?.label ?? readableFieldLabel(field);
}

export function resolveSender({ senderFields, draft, botName, botAvatarUrl, guildName, guildAvatarUrl }: PreviewSenderInput): PreviewSender {
  const enabled = senderFields.length > 0 && Boolean(draft["welcome.webhook.enabled"]);
  if (!enabled) return { enabled: false, name: botName, avatar: botAvatarUrl, badge: "BOT" };
  const nameMode = String(draft["welcome.webhook.name_mode"] || "server");
  const avatarMode = String(draft["welcome.webhook.avatar_mode"] || "server");
  const customName = String(draft["welcome.webhook.name"] || "").trim();
  const customAvatar = String(draft["welcome.webhook.avatar_url"] || "").trim();
  const name = nameMode === "fixed" ? (customName || botName)
    : nameMode === "member" ? "Novo membro"
      : nameMode === "inviter" ? "Quem convidou"
        : guildName || "Nome do servidor";
  const avatar = avatarMode === "custom" ? customAvatar
    : avatarMode === "server" ? guildAvatarUrl
      : avatarMode === "member" || avatarMode === "inviter" ? null
        : botAvatarUrl;
  return { enabled: true, name, avatar, badge: "APP" };
}

export function resolvePreviewRenderPlan(
  presentation: DashboardMessageEditorPresentation,
  sectionId: string | undefined,
  editorId: string | undefined,
  draft: Record<string, unknown>,
): PreviewRenderPlan {
  const isDm = editorId === "welcome-dm";
  const welcomeMode = String(draft[isDm ? "welcome.dm_render_mode" : "welcome.render_mode"] || "components_v2");
  const adaptiveWelcome = presentation === "adaptive" && sectionId === "welcome";
  const renderKind = presentation === "color_panel" ? "color-panel"
    : presentation === "components_v2" || (adaptiveWelcome && welcomeMode === "components_v2") ? "components-v2"
      : adaptiveWelcome && welcomeMode === "embed" ? "embed"
        : "generic";
  return { isDm, welcomeMode, adaptiveWelcome, renderKind };
}

export function filterPreviewFields(
  fields: DashboardFieldDefinition[],
  plan: Pick<PreviewRenderPlan, "adaptiveWelcome" | "isDm" | "welcomeMode">,
): DashboardFieldDefinition[] {
  if (!plan.adaptiveWelcome) return fields;
  return fields.filter((field) => {
    if (plan.isDm) return field.id.startsWith("welcome.dm.");
    if (plan.welcomeMode === "embed") return field.id.includes(".embed.");
    if (plan.welcomeMode === "normal" && field.id === "welcome.public.footer") return false;
    return !field.id.includes(".embed.");
  });
}
