import type {
  DashboardCommandCategory,
  DashboardCommandContext,
  DashboardCommandEntry,
  DashboardCommandsPayload,
  RawDashboardCommandEntry,
  RawDashboardCommandsCatalog,
} from "./dashboardCommandsTypes.js";

const UTILITIES_CATEGORY: DashboardCommandCategory = {
  key: "utilities",
  label: "Utilidades",
  emoji: "⌘",
  description: "Consulte a ajuda e confira o estado do bot.",
};

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function resolveUsage(rawUsage: unknown, context: DashboardCommandContext, category: string): string {
  const usage = String(rawUsage || "").replace(/\{([a-z0-9_]+)\}/gi, (match, key: string) => context.prefixes[key] ?? match);
  if (category !== "games" || context.gamesMode !== "commands") return usage;
  const prefix = context.prefixes.bot_prefix || "_";
  return usage.startsWith(prefix) ? usage : `${prefix}${usage}`;
}

function resolveAliases(entry: RawDashboardCommandEntry, context: DashboardCommandContext, category: string): string[] {
  const aliases = stringList(entry.aliases);
  const prefixed = String(entry.usage || "").includes("{bot_prefix}")
    || (category === "games" && context.gamesMode === "commands");
  if (!prefixed) return aliases;
  const prefix = context.prefixes.bot_prefix || "_";
  return aliases.map((alias) => alias.startsWith(prefix) ? alias : `${prefix}${alias}`);
}

export function buildDashboardCommandsPayload(
  catalog: RawDashboardCommandsCatalog,
  context: DashboardCommandContext,
  musicAvailable: boolean,
): DashboardCommandsPayload {
  const commands: DashboardCommandEntry[] = [];

  for (const entry of catalog.entries) {
    if (String(entry.permission || "user") !== "user" || entry.show_in_category === false) continue;
    const sourceCategory = entry.category === null || entry.category === undefined ? "utilities" : String(entry.category).trim();
    if (!sourceCategory || sourceCategory === "server" || (sourceCategory === "music" && !musicAvailable)) continue;
    const key = String(entry.key || "").trim();
    const description = String(entry.description || "").trim();
    const usage = resolveUsage(entry.usage, context, sourceCategory);
    if (!key || !description || !usage) continue;
    commands.push({
      key,
      category: sourceCategory,
      group: String(entry.group || "").trim() || UTILITIES_CATEGORY.label,
      description,
      usage,
      aliases: resolveAliases(entry, context, sourceCategory),
      keywords: stringList(entry.search_terms),
    });
  }

  const usedCategories = new Set(commands.map((entry) => entry.category));
  const categories = catalog.categories
    .map((category) => ({
      key: String(category.key || "").trim(),
      label: String(category.label || "").trim(),
      emoji: String(category.emoji || "").trim(),
      description: String(category.description || "").trim(),
    }))
    .filter((category) => category.key !== "server" && usedCategories.has(category.key));
  if (usedCategories.has(UTILITIES_CATEGORY.key)) categories.push(UTILITIES_CATEGORY);

  return { catalogVersion: catalog.version, musicAvailable, categories, commands };
}
