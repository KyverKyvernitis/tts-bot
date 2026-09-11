import type { DashboardCommandContext } from "../services/dashboardCommandsService.js";

export type DashboardFieldType =
  | "boolean"
  | "text"
  | "textarea"
  | "number"
  | "channel"
  | "role"
  | "role_multi"
  | "select"
  | "color"
  | "url"
  | "string_list"
  | "form_fields"
  | "color_slots"
  | "color_panel_layout";
export type DashboardFieldScope = "guild" | "welcome" | "birthday";

export interface DashboardFieldOption { value: string; label: string }
export type DashboardTemplateSyntax = "curly" | "dollar_curly";
export interface DashboardTemplateVariable { key: string; label: string }
export interface DashboardTemplateVariables { syntax: DashboardTemplateSyntax; items: DashboardTemplateVariable[] }
export type DashboardMessageEditorPresentation = "adaptive" | "components_v2" | "generic" | "color_panel";
export interface DashboardMessageEditorDefinition {
  id: string;
  label: string;
  description?: string;
  fieldIds: string[];
  senderFieldIds?: string[];
  presentation?: DashboardMessageEditorPresentation;
  variables?: DashboardTemplateVariables;
}
export interface DashboardGroupMetadata {
  kind?: "message";
  variables?: DashboardTemplateVariables;
  settingsFieldIds?: string[];
  editors?: DashboardMessageEditorDefinition[];
}

export interface DashboardFieldDefinition {
  id: string;
  label: string;
  description?: string;
  type: DashboardFieldType;
  scope: DashboardFieldScope;
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
  groupMetadata?: Record<string, DashboardGroupMetadata>;
  fields: DashboardFieldDefinition[];
}

export interface DashboardGuildSummary {
  guildId: string;
  sections: Array<{
    id: string;
    label: string;
    emoji: string;
    description: string;
    enabled: boolean | null;
    state: "active" | "inactive";
    configured: number;
    total: number;
    status: string;
    issues: string[];
  }>;
}

export class DashboardConfigValidationError extends Error {
  readonly code = "activation_requirements";

  constructor(readonly sectionId: string, readonly issues: string[]) {
    super(`Não foi possível ativar esta função. ${issues.join(" ")}`);
    this.name = "DashboardConfigValidationError";
  }
}

export class DashboardConfigValueError extends Error {
  readonly code = "invalid_setting";

  constructor(readonly fieldId: string, message: string) {
    super(message);
    this.name = "DashboardConfigValueError";
  }
}

export interface DashboardConfigService {
  listSections(): DashboardSectionDefinition[];
  getSummary(guildId: string): Promise<DashboardGuildSummary>;
  getSettings(guildId: string): Promise<{ guildId: string; sections: DashboardSectionDefinition[]; values: Record<string, unknown> }>;
  getCommandContext(guildId: string): Promise<DashboardCommandContext>;
  updateSettings(guildId: string, updates: Record<string, unknown>): Promise<{ ok: true; values: Record<string, unknown>; saved: string[]; revision?: number; changed_sections?: string[] }>;
}

export interface CreateDashboardConfigServiceOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName: string;
}
