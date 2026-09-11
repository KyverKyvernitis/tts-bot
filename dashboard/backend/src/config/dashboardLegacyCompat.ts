import { BIRTHDAY_DOC_CONFIG } from "./dashboardDocumentIds.js";
import { isLongLikeObject, isPlainObject } from "./dashboardObjectUtils.js";

function hasOwn(source: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(source, key);
}

export function hasStoredDashboardId(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "number") return Number.isFinite(value) && value > 0;
  if (typeof value === "string") {
    const text = value.trim();
    return Boolean(text && text !== "0");
  }
  if (isLongLikeObject(value)) {
    const text = String(value).trim();
    return Boolean(text && text !== "0");
  }
  return false;
}

export function applyLegacyFeatureFlags(type: string, raw: Record<string, unknown>, merged: Record<string, unknown>): void {
  if (type === "guild") {
    const rawForms = isPlainObject(raw.forms) ? raw.forms : {};
    const forms = isPlainObject(merged.forms) ? merged.forms : {};
    if (!hasOwn(rawForms, "enabled")) {
      forms.enabled = hasStoredDashboardId(forms.form_channel_id)
        && hasStoredDashboardId(forms.responses_channel_id)
        && hasStoredDashboardId(forms.active_message_id);
    }
    merged.forms = forms;

    const rawTickets = isPlainObject(raw.tickets) ? raw.tickets : {};
    const tickets = isPlainObject(merged.tickets) ? merged.tickets : {};
    if (!hasOwn(rawTickets, "feature_enabled")) {
      const panel = isPlainObject(tickets.panel) ? tickets.panel : {};
      tickets.feature_enabled = hasStoredDashboardId(panel.channel_id) && hasStoredDashboardId(panel.message_id);
    }
    merged.tickets = tickets;

    const rawColors = isPlainObject(raw.color_roles) ? raw.color_roles : {};
    const colors = isPlainObject(merged.color_roles) ? merged.color_roles : {};
    if (!hasOwn(rawColors, "enabled")) {
      const messageIds = Array.isArray(colors.message_ids) ? colors.message_ids : [];
      colors.enabled = hasStoredDashboardId(colors.channel_id) && messageIds.some(hasStoredDashboardId);
    }
    merged.color_roles = colors;

    if (!hasOwn(raw, "tts_enabled")) merged.tts_enabled = true;
  }

  if (type === BIRTHDAY_DOC_CONFIG && !hasOwn(raw, "enabled")) {
    merged.enabled = hasStoredDashboardId(merged.register_channel_id) || hasStoredDashboardId(merged.announce_channel_id);
  }
}
