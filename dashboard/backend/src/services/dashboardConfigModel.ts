import { allDashboardFields, dashboardSections } from "../config/dashboardCatalog.js";
import { isConfiguredValue, resolveDashboardSectionState } from "../config/dashboardSectionState.js";
import { normalizeColorPanelLayout, normalizeFieldValue, serializeFieldValue } from "../config/dashboardValueCodec.js";
import { DASHBOARD_PREFIX_FIELD_IDS, normalizeDashboardPrefix, validateDashboardPrefixes } from "../config/dashboardValidation.js";
import { dotSetForPath, getPath, isPlainObject, setPath } from "../config/dashboardObjectUtils.js";
import { DashboardConfigValidationError, DashboardConfigValueError, type DashboardFieldScope, type DashboardGuildSummary } from "../config/dashboardTypes.js";
import { snowflakeFromRaw } from "../config/dashboardSnowflakes.js";

export interface DashboardDocs {
  guild: Record<string, unknown>;
  welcome: Record<string, unknown>;
  birthday: Record<string, unknown>;
}

export interface DashboardUpdatePlan {
  patches: Map<DashboardFieldScope, Record<string, unknown>>;
  saved: string[];
  changedSections: string[];
  values: Record<string, unknown>;
}

export function dashboardValuesFromDocs(docs: DashboardDocs): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const field of allDashboardFields()) values[field.id] = serializeFieldValue(field, getPath(docs[field.scope], field.path));
  return values;
}

export function dashboardSummaryFromDocs(guildId: string, docs: DashboardDocs): DashboardGuildSummary {
  const values = dashboardValuesFromDocs(docs);
  return {
    guildId,
    sections: dashboardSections.map((section) => {
      const semantic = resolveDashboardSectionState(section.id, values);
      const configured = section.fields.filter((field) => isConfiguredValue(values[field.id])).length;
      return {
        id: section.id, label: section.label, emoji: section.emoji, description: section.description,
        enabled: semantic.enabled, state: semantic.state, configured, total: section.fields.length,
        status: semantic.status, issues: semantic.issues,
      };
    }),
  };
}

export function dashboardCommandContextFromGuild(guild: Record<string, unknown>) {
  const gamesMode = String(getPath(guild, "gincana_input_mode") || "triggers").trim().toLowerCase() === "commands"
    ? "commands" as const
    : "triggers" as const;
  return {
    gamesMode,
    prefixes: {
      bot_prefix: normalizeDashboardPrefix(getPath(guild, "bot_prefix")) || "_",
      atts_prefix: normalizeDashboardPrefix(getPath(guild, "atts_prefix")) || "%",
      teto_prefix: normalizeDashboardPrefix(getPath(guild, "teto_prefix")) || "'",
      gtts_prefix: normalizeDashboardPrefix(getPath(guild, "gtts_prefix")) || ".",
      edge_prefix: normalizeDashboardPrefix(getPath(guild, "edge_prefix")) || ",",
    },
  };
}

export function planDashboardUpdates(docs: DashboardDocs, updates: Record<string, unknown>): DashboardUpdatePlan {
  const fieldsById = new Map(allDashboardFields().map((field) => [field.id, field]));
  const patches = new Map<DashboardFieldScope, Record<string, unknown>>();
  const saved: string[] = [];
  const changedSections = new Set<string>();
  for (const [fieldId, rawValue] of Object.entries(updates || {}).slice(0, 250)) {
    const field = fieldsById.get(fieldId);
    if (!field) continue;
    const value = normalizeFieldValue(field, rawValue);
    if (field.id === "economy.staff_role_id" && String(value) === String(docs.guild.guild_id)) {
      throw new DashboardConfigValueError(field.id, "O cargo @everyone não pode administrar a economia.");
    }
    setPath(docs[field.scope], field.path, value);
    const scopePatch = patches.get(field.scope) ?? {};
    dotSetForPath(scopePatch, field.path, value);
    patches.set(field.scope, scopePatch);
    saved.push(field.id);
    changedSections.add(field.id.split(".")[0] || field.scope);
  }
  if (saved.some((fieldId) => DASHBOARD_PREFIX_FIELD_IDS.has(fieldId))) validateDashboardPrefixes(docs.guild);
  if (saved.includes("general.timezone")) changedSections.add("birthday");
  if (saved.includes("color_roles.panel_layout")) {
    const layout = normalizeColorPanelLayout(getPath(docs.guild, "color_roles.panel_layout"));
    const panelCount = layout.length;
    setPath(docs.guild, "color_roles.panel_layout", layout);
    setPath(docs.guild, "color_roles.panel_count", panelCount);
    const guildPatch = patches.get("guild") ?? {};
    dotSetForPath(guildPatch, "color_roles.panel_layout", layout);
    dotSetForPath(guildPatch, "color_roles.panel_count", panelCount);
    patches.set("guild", guildPatch);
  }

  const activationFields: Record<string, string> = {
    welcome: "welcome.enabled",
    forms: "forms.enabled",
    tickets: "tickets.feature_enabled",
    color_roles: "color_roles.enabled",
    birthday: "birthday.enabled",
    tts: "tts.enabled",
  };
  const nextValues = dashboardValuesFromDocs(docs);
  for (const [sectionId, fieldId] of Object.entries(activationFields)) {
    if (!saved.includes(fieldId) || !Boolean(nextValues[fieldId])) continue;
    const semantic = resolveDashboardSectionState(sectionId, nextValues);
    if (semantic.issues.length) throw new DashboardConfigValidationError(sectionId, semantic.issues);
  }

  if (saved.length && changedSections.has("welcome")) {
    const channelId = snowflakeFromRaw(getPath(docs.welcome, "channel_id"));
    const webhook = isPlainObject(getPath(docs.welcome, "webhook"))
      ? { ...(getPath(docs.welcome, "webhook") as Record<string, unknown>) }
      : {};
    webhook.channel_id = channelId;
    setPath(docs.welcome, "webhook", webhook);
    const welcomePatch = patches.get("welcome") ?? {};
    dotSetForPath(welcomePatch, "webhook.channel_id", webhook.channel_id);
    patches.set("welcome", welcomePatch);
  }
  return { patches, saved, changedSections: Array.from(changedSections), values: dashboardValuesFromDocs(docs) };
}
