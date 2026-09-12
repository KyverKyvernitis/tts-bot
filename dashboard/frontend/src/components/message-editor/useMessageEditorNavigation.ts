import type { Dispatch, SetStateAction } from "react";
import type { DashboardFieldDefinition, DashboardMessageEditorPresentation } from "../../types/dashboard";
import {
  messageEditorCanInlineEdit,
  messageEditorSelectFieldAction,
} from "./messageEditorInteractionModel";
import {
  relatedMessageEditorContextFields,
  type MessageEditorContextPlacement,
  type MessageEditorTextSelection,
  type MessageEditorView,
} from "./messageEditorModel";

interface UseMessageEditorNavigationOptions {
  fields: DashboardFieldDefinition[];
  visualFields: DashboardFieldDefinition[];
  senderFields: DashboardFieldDefinition[];
  senderFieldIdSet: Set<string>;
  presentation: DashboardMessageEditorPresentation;
  jsonDirty: boolean;
  pendingJsonChanges: unknown;
  view: MessageEditorView;
  selectedFieldId: string | null;
  editingFieldId: string | null;
  textSelectionRef: { current: MessageEditorTextSelection | null };
  setView: Dispatch<SetStateAction<MessageEditorView>>;
  setSelectedFieldId: Dispatch<SetStateAction<string | null>>;
  setEditingFieldId: Dispatch<SetStateAction<string | null>>;
  setContextAnchorFieldId: Dispatch<SetStateAction<string | null>>;
  setContextPlacement: Dispatch<SetStateAction<MessageEditorContextPlacement | null>>;
  setActiveTextFieldId: Dispatch<SetStateAction<string | null>>;
  clearTextSelection(): void;
  discardJson(): void;
}

export function useMessageEditorNavigation({
  fields,
  visualFields,
  senderFields,
  senderFieldIdSet,
  presentation,
  jsonDirty,
  pendingJsonChanges,
  view,
  selectedFieldId,
  editingFieldId,
  textSelectionRef,
  setView,
  setSelectedFieldId,
  setEditingFieldId,
  setContextAnchorFieldId,
  setContextPlacement,
  setActiveTextFieldId,
  clearTextSelection,
  discardJson,
}: UseMessageEditorNavigationOptions) {
  const blocked = Boolean(jsonDirty || pendingJsonChanges);
  const selectedField = fields.find((field) => field.id === selectedFieldId) ?? null;
  const senderPrimaryField = senderFields.find((field) => field.id === "welcome.webhook.enabled") ?? senderFields[0] ?? null;
  const senderSelected = Boolean(selectedField && senderFieldIdSet.has(selectedField.id));
  const inspectorFields = relatedMessageEditorContextFields(selectedField, visualFields);

  function openSenderInspector() {
    if (!senderPrimaryField || blocked) return;
    setSelectedFieldId(senderPrimaryField.id);
    setContextAnchorFieldId(senderPrimaryField.id);
    setEditingFieldId(null);
    setActiveTextFieldId(null);
    setView("inspector");
  }

  function handleSelectField(field: DashboardFieldDefinition) {
    const action = messageEditorSelectFieldAction({
      field,
      blocked,
      presentation,
      currentEditingFieldId: editingFieldId,
      currentTextSelectionFieldId: textSelectionRef.current?.fieldId ?? null,
    });
    if (action.kind === "blocked") {
      setView(action.view);
      return;
    }
    setSelectedFieldId(action.selectedFieldId);
    setEditingFieldId(action.editingFieldId);
    setContextAnchorFieldId(action.contextAnchorFieldId);
    setActiveTextFieldId(action.activeTextFieldId);
    setView(action.view);
    if (action.clearTextSelection) clearTextSelection();
  }

  function handleEditField(field: DashboardFieldDefinition) {
    if (!messageEditorCanInlineEdit(field, blocked)) return;
    setSelectedFieldId(field.id);
    setContextAnchorFieldId(null);
    setActiveTextFieldId(field.id);
    setView("canvas");
    setEditingFieldId(field.id);
  }

  function openVariables() {
    setEditingFieldId(null);
    setContextPlacement(null);
    setContextAnchorFieldId(null);
    setView("variables");
  }

  function openJson() {
    setEditingFieldId(null);
    setContextPlacement(null);
    setContextAnchorFieldId(null);
    setView("json");
  }

  function openInspector(field = selectedField) {
    if (!field || blocked) return;
    setSelectedFieldId(field.id);
    setContextAnchorFieldId(field.id);
    setEditingFieldId(null);
    setView("inspector");
  }

  function leaveAuxiliaryView() {
    if (pendingJsonChanges) return;
    if (view === "json" && jsonDirty) {
      if (!window.confirm("Descartar as alterações não aplicadas do JSON?")) return;
      discardJson();
    }
    setContextPlacement(null);
    setContextAnchorFieldId(null);
    setView("canvas");
  }

  return {
    blocked,
    selectedField,
    senderPrimaryField,
    senderSelected,
    inspectorFields,
    openSenderInspector,
    handleSelectField,
    handleEditField,
    openVariables,
    openJson,
    openInspector,
    leaveAuxiliaryView,
  };
}
