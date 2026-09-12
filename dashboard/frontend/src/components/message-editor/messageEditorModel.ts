import type { DashboardFieldDefinition } from "../../types/dashboard";

export interface MessageEditorHistoryChange {
  field: DashboardFieldDefinition;
  before: unknown;
  after: unknown;
}

export interface MessageEditorHistoryEntry {
  changes: MessageEditorHistoryChange[];
  mergeKey: string | null;
  at: number;
}

export type MessageEditorView = "canvas" | "inspector" | "variables" | "json";

export interface MessageEditorContextPlacement {
  left: number;
  top: number;
  width: number;
  side: "left" | "right" | "over";
}

export interface MessageEditorTextSelection {
  fieldId: string;
  start: number;
  end: number;
}

export interface MessageEditorTextEditResult {
  value: string;
  start: number;
  end: number;
}

export function messageEditorValuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function cloneMessageEditorValue<T>(value: T): T {
  if (value === undefined || value === null || typeof value !== "object") return value;
  try { return structuredClone(value); } catch {
    try { return JSON.parse(JSON.stringify(value)) as T; } catch { return value; }
  }
}

export function messageEditorVisualFieldVisible(editorId: string, fieldId: string, draft: Record<string, unknown>): boolean {
  if (fieldId.startsWith("welcome.webhook.")) {
    if (fieldId === "welcome.webhook.enabled") return true;
    if (!Boolean(draft["welcome.webhook.enabled"])) return false;
    if (fieldId === "welcome.webhook.name") return String(draft["welcome.webhook.name_mode"] || "fixed") === "fixed";
    if (fieldId === "welcome.webhook.avatar_url") return String(draft["welcome.webhook.avatar_mode"] || "server") === "custom";
    return true;
  }
  if (editorId === "welcome-public") {
    const renderMode = String(draft["welcome.render_mode"] || "components_v2");
    if (["welcome.style", "welcome.accent_color", "welcome.accent_color_mode", "welcome.media_mode", "welcome.media_url"].includes(fieldId)) {
      if (renderMode !== "components_v2") return false;
      if (fieldId === "welcome.accent_color") return String(draft["welcome.accent_color_mode"] || "fixed") === "fixed";
      if (fieldId === "welcome.media_url") return String(draft["welcome.media_mode"] || "custom") === "custom";
      return true;
    }
    if (fieldId.includes(".embed.")) {
      if (renderMode !== "embed") return false;
      if (fieldId === "welcome.embed.color") return String(draft["welcome.embed.color_mode"] || "fixed") === "fixed";
      if (fieldId === "welcome.embed.author_icon_url") return String(draft["welcome.embed.author_icon_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.thumbnail_url") return String(draft["welcome.embed.thumbnail_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.image_url") return String(draft["welcome.embed.image_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.footer_icon_url") return String(draft["welcome.embed.footer_icon_mode"] || "none") === "custom";
      return true;
    }
    if (fieldId.includes(".public.")) return renderMode !== "embed";
  }
  return true;
}

export function relatedMessageEditorContextFields(
  selected: DashboardFieldDefinition | null,
  fields: DashboardFieldDefinition[],
): DashboardFieldDefinition[] {
  if (!selected) return [];
  const id = selected.id;
  if (id.startsWith("welcome.webhook.")) {
    return fields.filter((field) => field.id.startsWith("welcome.webhook."));
  }
  if (["welcome.style", "welcome.accent_color", "welcome.accent_color_mode"].includes(id)) {
    return fields.filter((field) => ["welcome.style", "welcome.accent_color", "welcome.accent_color_mode"].includes(field.id));
  }
  if (["welcome.media_mode", "welcome.media_url"].includes(id)) {
    return fields.filter((field) => ["welcome.media_mode", "welcome.media_url"].includes(field.id));
  }
  const groups: RegExp[] = [
    /^(.*\.author_)(?:name|icon_mode|icon_url|url)$/,
    /^(.*\.footer_)(?:text|icon_mode|icon_url)$/,
    /^(.*\.image_)(?:mode|url)$/,
    /^(.*\.thumbnail_)(?:mode|url)$/,
    /^(.*\.color)(?:_mode)?$/,
    /^(.*\.title)(?:_url)?$/,
  ];
  for (const pattern of groups) {
    const match = id.match(pattern);
    if (!match) continue;
    const prefix = match[1];
    const related = fields.filter((field) => field.id.startsWith(prefix));
    if (related.length) return related;
  }

  const actionMatch = id.match(/^(.*?)(approve|reject|button)(?:_|$)/);
  if (actionMatch) {
    const prefix = `${actionMatch[1]}${actionMatch[2]}`;
    const related = fields.filter((field) => field.id.startsWith(prefix));
    if (related.length) return related;
  }

  return [selected];
}

function normalizedSelection(value: string, selection?: Pick<MessageEditorTextSelection, "start" | "end"> | null): [number, number] {
  const start = Math.max(0, Math.min(value.length, selection?.start ?? value.length));
  const end = Math.max(start, Math.min(value.length, selection?.end ?? start));
  return [start, end];
}

export function messageEditorTextLimitNotice(field: DashboardFieldDefinition, value: string): string | null {
  if (field.maxLength === undefined || value.length <= field.maxLength) return null;
  return `Limite de ${field.maxLength} caracteres`;
}

export function replaceMessageEditorText(
  value: string,
  insertion: string,
  selection?: Pick<MessageEditorTextSelection, "start" | "end"> | null,
  selectInserted = false,
): MessageEditorTextEditResult {
  const [start, end] = normalizedSelection(value, selection);
  const next = `${value.slice(0, start)}${insertion}${value.slice(end)}`;
  return {
    value: next,
    start: selectInserted ? start : start + insertion.length,
    end: start + insertion.length,
  };
}

export function wrapMessageEditorText(
  value: string,
  prefix: string,
  suffix: string,
  placeholder: string,
  selection?: Pick<MessageEditorTextSelection, "start" | "end"> | null,
): MessageEditorTextEditResult {
  const [start, end] = normalizedSelection(value, selection);
  const selected = value.slice(start, end) || placeholder;
  const insertion = `${prefix}${selected}${suffix}`;
  return {
    value: `${value.slice(0, start)}${insertion}${value.slice(end)}`,
    start: start + prefix.length,
    end: start + prefix.length + selected.length,
  };
}

export function prefixMessageEditorTextLines(
  value: string,
  prefix: string,
  placeholder: string,
  selection?: Pick<MessageEditorTextSelection, "start" | "end"> | null,
): MessageEditorTextEditResult {
  const [start, end] = normalizedSelection(value, selection);
  const selected = value.slice(start, end) || placeholder;
  const insertion = selected.split("\n").map((line) => `${prefix}${line}`).join("\n");
  return {
    value: `${value.slice(0, start)}${insertion}${value.slice(end)}`,
    start: start + prefix.length,
    end: start + insertion.length,
  };
}
