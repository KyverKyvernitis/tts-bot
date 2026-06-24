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
  group?: string;
}

export interface DashboardSectionDefinition {
  id: string;
  label: string;
  emoji: string;
  description: string;
  groups?: string[];
  fields: DashboardFieldDefinition[];
  actions?: Array<{ id: string; label: string; description?: string; group?: string }>;
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
  user?: { id: string; username?: string | null; global_name?: string | null; avatar?: string | null; avatarUrl?: string | null };
  sections?: Array<{ id: string; label: string; emoji: string; description: string }>;
  error?: string;
}

export interface DashboardUserPayload {
  id: string;
  username?: string | null;
  global_name?: string | null;
  avatar?: string | null;
  avatarUrl?: string | null;
}

export interface DashboardSessionPayload {
  ok: boolean;
  authenticated?: boolean;
  user?: DashboardUserPayload | null;
  error?: string;
}

export interface DashboardServerCard {
  id: string;
  name: string;
  icon: string | null;
  owner: boolean;
  permissions: string;
  botPresent: boolean;
  canManage: boolean;
  canInvite: boolean;
  reason: string;
  inviteUrl?: string | null;
}

export interface DashboardServersPayload {
  ok: boolean;
  user?: DashboardUserPayload | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  error?: string;
}

export interface DashboardInvitePayload {
  ok: boolean;
  guild_id?: string;
  invite_url?: string;
  error?: string;
}

export interface DashboardChannelOption {
  id: string;
  name: string;
  type: number;
  parentId?: string | null;
}

export interface DashboardRoleOption {
  id: string;
  name: string;
  color?: number;
}

export interface DashboardOptionsPayload {
  ok: boolean;
  guildId?: string;
  channels: DashboardChannelOption[];
  roles: DashboardRoleOption[];
  error?: string;
}
