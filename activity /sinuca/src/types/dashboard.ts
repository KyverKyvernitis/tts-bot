export type DashboardFieldType = "boolean" | "text" | "textarea" | "number" | "channel" | "role" | "select" | "color" | "url";

export interface DashboardFieldOption {
  value: string;
  label: string;
}

export interface DashboardFieldDefinition {
  id: string;
  label: string;
  description?: string;
  type: DashboardFieldType;
  scope: "guild" | "welcome" | "birthday";
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

export interface DashboardSectionSummary {
  id: string;
  label: string;
  emoji: string;
  description: string;
  enabled: boolean | null;
  configured: number;
  total: number;
  status: string;
}

export interface DashboardSettingsPayload {
  ok: boolean;
  guildId: string;
  sections: DashboardSectionDefinition[];
  values: Record<string, unknown>;
  error?: string;
}

export interface DashboardSummaryPayload {
  ok: boolean;
  guildId: string;
  sections: DashboardSectionSummary[];
  error?: string;
}

export interface DashboardBootstrapPayload {
  ok: boolean;
  guild_id?: string;
  user?: { id: string; username?: string | null; global_name?: string | null; avatar?: string | null };
  sections?: Array<{ id: string; label: string; emoji: string; description: string }>;
  error?: string;
}
