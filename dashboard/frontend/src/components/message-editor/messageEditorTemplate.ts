import type { DashboardFieldDefinition, DashboardTemplateSyntax } from "../../types/dashboard";

export function formatTemplateVariable(syntax: DashboardTemplateSyntax, key: string): string {
  return syntax === "dollar_curly" ? `\${${key}}` : `{${key}}`;
}

export function readableFieldLabel(field: DashboardFieldDefinition): string {
  return field.label
    .replace(/^Embed:\s*/i, "")
    .replace(/^DM:\s*/i, "")
    .trim();
}
