import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardTemplateVariables,
} from "../../types/dashboard";

export type MessageEditorMode = "visual" | "json" | "variables";
export type MessageEditorMobileView = "edit" | "preview";

export interface MessageEditorProps {
  sectionId: string;
  sectionLabel: string;
  groupLabel: string;
  fields: DashboardFieldDefinition[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  botName?: string;
  botAvatarUrl?: string | null;
  variables?: DashboardTemplateVariables;
  hasUnsavedChanges: boolean;
  applying: boolean;
  onChange(field: DashboardFieldDefinition, raw: string | boolean): void;
  onApply(): void | Promise<void>;
  onBack(): void;
}

export interface JsonFieldChange {
  field: DashboardFieldDefinition;
  raw: string | boolean;
  expected: unknown;
}
