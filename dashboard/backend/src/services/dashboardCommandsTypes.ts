export interface DashboardCommandContext {
  prefixes: Record<string, string>;
  gamesMode: "commands" | "triggers";
}

export interface DashboardCommandCategory {
  key: string;
  label: string;
  emoji: string;
  description: string;
}

export interface DashboardCommandEntry {
  key: string;
  category: string;
  group: string;
  description: string;
  usage: string;
  aliases: string[];
  keywords: string[];
}

export interface DashboardCommandsPayload {
  catalogVersion: number;
  musicAvailable: boolean;
  categories: DashboardCommandCategory[];
  commands: DashboardCommandEntry[];
}

export interface RawDashboardCommandCategory {
  key?: unknown;
  label?: unknown;
  emoji?: unknown;
  description?: unknown;
}

export interface RawDashboardCommandEntry {
  key?: unknown;
  category?: unknown;
  group?: unknown;
  description?: unknown;
  usage?: unknown;
  search_terms?: unknown;
  aliases?: unknown;
  permission?: unknown;
  show_in_category?: unknown;
}

export interface RawDashboardCommandsCatalog {
  version: number;
  categories: RawDashboardCommandCategory[];
  entries: RawDashboardCommandEntry[];
}
