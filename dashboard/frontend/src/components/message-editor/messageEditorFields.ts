import type { DashboardFieldDefinition } from "../../types/dashboard";
import { messageEditorValuesEqual, messageEditorVisualFieldVisible } from "./messageEditorModel";

export interface MessageEditorFieldGroups {
  jsonFields: DashboardFieldDefinition[];
  visualFields: DashboardFieldDefinition[];
  senderFields: DashboardFieldDefinition[];
  messageFields: DashboardFieldDefinition[];
}

export function messageEditorFieldGroups(
  fields: DashboardFieldDefinition[],
  senderFieldIds: string[],
  editorId: string,
  draft: Record<string, unknown>,
  revealedFieldId?: string | null,
): MessageEditorFieldGroups {
  const senderIds = new Set(senderFieldIds);
  const jsonFields = fields.filter((field) => field.type !== "color_slots" && field.type !== "color_panel_layout");
  const visualFields = fields.filter((field) => field.id === revealedFieldId || messageEditorVisualFieldVisible(editorId, field.id, draft));
  return {
    jsonFields,
    visualFields,
    senderFields: visualFields.filter((field) => senderIds.has(field.id)),
    messageFields: visualFields.filter((field) => !senderIds.has(field.id)),
  };
}

export function messageEditorHasFieldChanges(
  fields: DashboardFieldDefinition[],
  baseline: Record<string, unknown>,
  draft: Record<string, unknown>,
): boolean {
  return fields.some((field) => !messageEditorValuesEqual(baseline[field.id], draft[field.id]));
}
