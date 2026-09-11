import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { RawDashboardCommandsCatalog } from "./dashboardCommandsTypes.js";

let catalogCache: RawDashboardCommandsCatalog | null = null;

function catalogCandidates(): string[] {
  return [
    fileURLToPath(new URL("../data/help_catalog.json", import.meta.url)),
    fileURLToPath(new URL("../../../../shared/help_catalog.json", import.meta.url)),
  ];
}

export function loadDashboardCommandsCatalog(): RawDashboardCommandsCatalog {
  if (catalogCache) return catalogCache;
  const path = catalogCandidates().find((candidate) => existsSync(candidate));
  if (!path) throw new Error("help_catalog_missing");
  const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<RawDashboardCommandsCatalog>;
  if (parsed.version !== 1 || !Array.isArray(parsed.categories) || !Array.isArray(parsed.entries)) {
    throw new Error("help_catalog_invalid");
  }
  catalogCache = { version: parsed.version, categories: parsed.categories, entries: parsed.entries };
  return catalogCache;
}

export function resetDashboardCommandsCatalogCacheForTests(): void {
  catalogCache = null;
}
