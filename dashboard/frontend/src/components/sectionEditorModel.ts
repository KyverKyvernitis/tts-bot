import type {
  DashboardFieldDefinition,
  DashboardMessageEditorDefinition,
  DashboardSectionDefinition,
  DashboardTemplateVariables,
} from "../types/dashboard";

export function sectionEditorValuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function sectionEditorEnabled(sectionId: string, group: string, draft: Record<string, unknown>): boolean {
  if (sectionId === "welcome" && group === "Mensagem de entrada") return Boolean(draft["welcome.enabled"]);
  if (sectionId === "welcome" && group === "Mensagem privada") return Boolean(draft["welcome.enabled"]) && Boolean(draft["welcome.dm_enabled"]);
  if (sectionId === "forms" && group === "Aprovação") return Boolean(draft["forms.approval.enabled"]);
  return true;
}

export function sectionEditorFieldVisible(
  sectionId: string,
  field: DashboardFieldDefinition,
  draft: Record<string, unknown>,
): boolean {
  if (field.id === "tts.ignored_tts_role_enabled") return false;
  if (sectionId === "tts" && field.id === "tts.engine") return false;

  if (sectionId === "welcome") {
    if (field.id === "welcome.style") return String(draft["welcome.render_mode"] || "") === "components_v2";
    if (field.id === "welcome.accent_color") return String(draft["welcome.accent_color_mode"] || "fixed") === "fixed";
    if (field.id === "welcome.media_url") return String(draft["welcome.media_mode"] || "") === "custom";
    if (field.id.startsWith("welcome.webhook.") && field.id !== "welcome.webhook.enabled" && !Boolean(draft["welcome.webhook.enabled"])) return false;
    if (field.id === "welcome.webhook.name") return String(draft["welcome.webhook.name_mode"] || "") === "fixed";
    if (field.id === "welcome.webhook.avatar_url") return String(draft["welcome.webhook.avatar_mode"] || "") === "custom";
  }

  return true;
}

export function createLegacyMessageEditor(group: string, fields: DashboardFieldDefinition[]): DashboardMessageEditorDefinition {
  return {
    id: `legacy-${group.toLocaleLowerCase("pt-BR").replace(/[^a-z0-9]+/g, "-")}`,
    label: group,
    description: "Conteúdo e aparência desta mensagem.",
    fieldIds: fields.map((field) => field.id),
  };
}


export interface ActiveSectionMessageEditor {
  id: string;
  label: string;
  description?: string;
  fields: DashboardFieldDefinition[];
  senderFieldIds?: string[];
  presentation?: DashboardMessageEditorDefinition["presentation"];
  variables?: DashboardTemplateVariables;
  baseline: Record<string, unknown>;
}

export function resolveSectionMessageEditor(
  section: DashboardSectionDefinition,
  editor: DashboardMessageEditorDefinition,
  draft: Record<string, unknown>,
  fallbackVariables?: DashboardTemplateVariables,
): ActiveSectionMessageEditor | null {
  const requestedFieldIds = [...editor.fieldIds, ...(editor.senderFieldIds ?? [])];
  const editorFields = requestedFieldIds
    .map((id) => section.fields.find((field) => field.id === id))
    .filter((field): field is DashboardFieldDefinition => Boolean(field));
  const colorPanelMatch = section.id === "color_roles" ? editor.id.match(/^color-panel-([1-3])$/) : null;
  if (colorPanelMatch) {
    const slotsField = section.fields.find((field) => field.id === "color_roles.slots");
    if (slotsField && !editorFields.some((field) => field.id === slotsField.id)) editorFields.push(slotsField);
  }
  if (!editorFields.length) return null;
  return {
    id: editor.id,
    label: editor.label,
    description: editor.description,
    fields: editorFields,
    senderFieldIds: editor.senderFieldIds,
    presentation: editor.presentation,
    variables: editor.variables ?? fallbackVariables,
    baseline: Object.fromEntries(editorFields.map((field) => [field.id, draft[field.id]])),
  };
}
