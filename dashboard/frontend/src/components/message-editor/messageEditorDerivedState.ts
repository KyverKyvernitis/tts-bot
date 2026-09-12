import { colorPanelFromEditorId, normalizeColorPanelLayout } from "../color-roles/colorRolesModel";
import { messageEditorContextCopy } from "./messageEditorInteractionModel";
import type { DashboardFieldDefinition, DashboardMessageEditorPresentation } from "../../types/dashboard";
import type { MessageEditorView } from "./messageEditorModel";

export interface MessageEditorDerivedStateInput {
  sectionId: string;
  editorId: string;
  draft: Record<string, unknown>;
  jsonDirty: boolean;
  pendingJsonChanges: boolean;
  view: MessageEditorView;
  senderSelected: boolean;
  presentation: DashboardMessageEditorPresentation;
  selectedColorSlot: number | null;
  inspectorFields: DashboardFieldDefinition[];
  selectedField: DashboardFieldDefinition | null;
  activeTextField: DashboardFieldDefinition | null;
}

export function messageEditorDerivedState(input: MessageEditorDerivedStateInput) {
  const colorPanelLayout = input.sectionId === "color_roles"
    ? normalizeColorPanelLayout(input.draft["color_roles.panel_layout"])
    : [];
  const activeColorPanel = input.sectionId === "color_roles"
    ? colorPanelFromEditorId(colorPanelLayout, input.editorId)
    : null;
  const context = messageEditorContextCopy({
    view: input.view,
    senderSelected: input.senderSelected,
    presentation: input.presentation,
    selectedColorSlot: input.selectedColorSlot,
    activeColorPanelSlots: activeColorPanel?.slots,
    inspectorLabel: input.inspectorFields[0]?.label,
    selectedFieldLabel: input.selectedField?.label,
    activeTextFieldLabel: input.activeTextField?.label,
  });
  return {
    activeColorPanel,
    colorSlotIds: activeColorPanel?.slots ?? null,
    senderEnabled: Boolean(input.draft["welcome.webhook.enabled"]),
    applyDisabled: Boolean(input.pendingJsonChanges),
    canvasInteractive: !input.jsonDirty && !input.pendingJsonChanges,
    contextTitle: context.title,
    contextDescription: context.description,
  };
}
