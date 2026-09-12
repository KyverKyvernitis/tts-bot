import type { DashboardFieldDefinition, DashboardMessageEditorPresentation } from "../../types/dashboard";
import type { MessageEditorView } from "./messageEditorModel";

export type MessageEditorSelectionAction =
  | { kind: "blocked"; view: "json" }
  | {
      kind: "selected";
      selectedFieldId: string;
      editingFieldId: string | null;
      contextAnchorFieldId: string | null;
      activeTextFieldId: string | null;
      view: "canvas" | "inspector";
      clearTextSelection: boolean;
    };

export function messageEditorSelectFieldAction(options: {
  field: DashboardFieldDefinition;
  blocked: boolean;
  presentation: DashboardMessageEditorPresentation;
  currentEditingFieldId: string | null;
  currentTextSelectionFieldId: string | null;
}): MessageEditorSelectionAction {
  const { field, blocked, presentation, currentEditingFieldId, currentTextSelectionFieldId } = options;
  if (blocked) return { kind: "blocked", view: "json" };

  const editingFieldId = currentEditingFieldId === field.id ? currentEditingFieldId : null;
  if (presentation === "color_panel" && field.id === "color_roles.slots") {
    return {
      kind: "selected",
      selectedFieldId: field.id,
      editingFieldId,
      contextAnchorFieldId: null,
      activeTextFieldId: null,
      view: "canvas",
      clearTextSelection: false,
    };
  }

  if (field.type === "text" || field.type === "textarea") {
    return {
      kind: "selected",
      selectedFieldId: field.id,
      editingFieldId,
      contextAnchorFieldId: null,
      activeTextFieldId: field.id,
      view: "canvas",
      clearTextSelection: currentTextSelectionFieldId !== field.id,
    };
  }

  return {
    kind: "selected",
    selectedFieldId: field.id,
    editingFieldId,
    contextAnchorFieldId: field.id,
    activeTextFieldId: null,
    view: "inspector",
    clearTextSelection: true,
  };
}

export function messageEditorCanInlineEdit(field: DashboardFieldDefinition, blocked: boolean): boolean {
  return !blocked && (field.type === "text" || field.type === "textarea");
}

export function messageEditorContextCopy(options: {
  view: MessageEditorView;
  senderSelected: boolean;
  presentation: DashboardMessageEditorPresentation;
  selectedColorSlot: number | null;
  activeColorPanelSlots?: number[] | null;
  inspectorLabel?: string | null;
  selectedFieldLabel?: string | null;
  activeTextFieldLabel?: string | null;
}): { title: string; description: string } {
  const {
    view,
    senderSelected,
    presentation,
    selectedColorSlot,
    activeColorPanelSlots,
    inspectorLabel,
    selectedFieldLabel,
    activeTextFieldLabel,
  } = options;

  if (view === "variables") {
    return {
      title: "Variáveis",
      description: activeTextFieldLabel ? `Inserir em ${activeTextFieldLabel}` : "Toque em uma variável para copiar",
    };
  }
  if (view === "json") return { title: "JSON avançado", description: "Edição técnica da mensagem" };
  if (senderSelected) return { title: "Remetente da mensagem", description: "Nome e avatar usados no envio" };
  if (presentation === "color_panel" && selectedColorSlot) {
    const index = Math.max(1, (activeColorPanelSlots?.indexOf(selectedColorSlot) ?? 0) + 1);
    return { title: `Opção ${index}`, description: "Alterações aparecem imediatamente na prévia" };
  }
  return {
    title: inspectorLabel ?? selectedFieldLabel ?? "Propriedades",
    description: "Alterações aparecem imediatamente na prévia",
  };
}
