import {
  normalizeColorPanelLayout,
  normalizeColorSlots,
  normalizeDashboardColor,
  serializeColorSlots,
} from "./dashboardColorValueCodec.js";
import { normalizeFormFields } from "./dashboardFormValueCodec.js";
import { snowflakeFromRaw, snowflakeToLong, serializeSnowflake } from "./dashboardSnowflakes.js";
import { type DashboardFieldDefinition } from "./dashboardTypes.js";
import { DASHBOARD_PREFIX_FIELD_IDS, normalizeDashboardPrefix, normalizeDashboardTimeZone } from "./dashboardValidation.js";

export { normalizeColorPanelLayout } from "./dashboardColorValueCodec.js";

export function serializeFieldValue(field: DashboardFieldDefinition, value: unknown): unknown {
  if (field.type === "channel" || field.type === "role") return serializeSnowflake(value);
  if (field.type === "role_multi") return Array.isArray(value) ? value.map(serializeSnowflake).filter(Boolean) : [];
  if (field.type === "color_slots") return serializeColorSlots(value);
  if (field.type === "color_panel_layout") return normalizeColorPanelLayout(value);
  return value;
}

export function normalizeFieldValue(field: DashboardFieldDefinition, raw: unknown): unknown {
  if (field.type === "boolean") return raw === true || raw === "true" || raw === "1" || raw === 1;
  if (field.type === "number") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return field.min ?? 0;
    return Math.max(field.min ?? Number.MIN_SAFE_INTEGER, Math.min(field.max ?? Number.MAX_SAFE_INTEGER, Math.trunc(n)));
  }
  if (field.type === "channel" || field.type === "role") return snowflakeFromRaw(raw);
  if (field.type === "role_multi") {
    const values = Array.isArray(raw) ? raw : String(raw ?? "").split(/[\s,;]+/);
    const seen = new Set<string>();
    return values.map((item) => serializeSnowflake(snowflakeFromRaw(item))).filter((item) => item && !seen.has(item) && Boolean(seen.add(item))).slice(0, 25).map(snowflakeToLong);
  }
  if (field.type === "select") {
    const value = String(raw ?? "").trim();
    const allowed = new Set((field.options ?? []).map((item) => item.value));
    return allowed.has(value) ? value : ((field.options ?? [])[0]?.value ?? "");
  }
  if (field.type === "color") return normalizeDashboardColor(raw);
  if (field.type === "url") {
    let value = String(raw ?? "").trim();
    if ((value.startsWith("<") && value.endsWith(">"))
      || (value.startsWith('"') && value.endsWith('"'))
      || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1).trim();
    value = value.replace(/&amp;/gi, "&");
    if (!value) return "";
    try {
      const parsed = new URL(value);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
      return value.slice(0, field.maxLength ?? 1000);
    } catch {
      return "";
    }
  }
  if (field.type === "string_list") {
    const list = Array.isArray(raw) ? raw : String(raw ?? "").split(/\r?\n/);
    return Array.from(new Set(list.map((item) => String(item).trim().slice(0, 80)).filter(Boolean))).slice(0, 40);
  }
  if (field.type === "form_fields") return normalizeFormFields(raw);
  if (field.type === "color_slots") return normalizeColorSlots(raw);
  if (field.type === "color_panel_layout") return normalizeColorPanelLayout(raw);
  if (field.id === "general.timezone") return normalizeDashboardTimeZone(raw);
  if (DASHBOARD_PREFIX_FIELD_IDS.has(field.id)) return normalizeDashboardPrefix(raw, field.maxLength ?? 8);
  return String(raw ?? "").slice(0, field.maxLength ?? (field.type === "textarea" ? 1800 : 300));
}
