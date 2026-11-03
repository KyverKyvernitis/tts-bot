import { useEffect, useMemo, useRef, useState, type MutableRefObject, type RefObject } from "react";
import type { DashboardFieldDefinition, DashboardTemplateVariables } from "../../types/dashboard";
import {
  formatTemplateVariable,
} from "./messageEditorUtils";
import {
  messageEditorTextLimitNotice,
  prefixMessageEditorTextLines,
  replaceMessageEditorText,
  wrapMessageEditorText,
  type MessageEditorTextEditResult,
  type MessageEditorTextSelection,
  type MessageEditorView,
} from "./messageEditorModel";

interface UseMessageEditorTextEditingOptions {
  fields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  variables?: DashboardTemplateVariables;
  editingFieldId: string | null;
  dialogRef: RefObject<HTMLDivElement>;
  latestDraftRef: MutableRefObject<Record<string, unknown>>;
  recordChanges(changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>, merge?: boolean): void;
  setSelectedFieldId(value: string | null): void;
  setEditingFieldId(value: string | null): void;
  setView(value: MessageEditorView): void;
}

export function useMessageEditorTextEditing(options: UseMessageEditorTextEditingOptions) {
  const {
    fields,
    draft,
    variables,
    editingFieldId,
    dialogRef,
    latestDraftRef,
    recordChanges,
    setSelectedFieldId,
    setEditingFieldId,
    setView,
  } = options;
  const [activeTextFieldId, setActiveTextFieldId] = useState<string | null>(null);
  const [textToolNotice, setTextToolNotice] = useState<string | null>(null);
  const textSelectionRef = useRef<MessageEditorTextSelection | null>(null);
  const selectionFrameRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (selectionFrameRef.current !== null) window.cancelAnimationFrame(selectionFrameRef.current);
  }, []);

  const activeTextField = useMemo(
    () => fields.find((field) => field.id === activeTextFieldId && (field.type === "text" || field.type === "textarea")) ?? null,
    [activeTextFieldId, fields],
  );
  const activeEditingField = useMemo(
    () => editingFieldId
      ? fields.find((field) => field.id === editingFieldId && (field.type === "text" || field.type === "textarea")) ?? null
      : null,
    [editingFieldId, fields],
  );
  const activeEditingValue = activeEditingField ? String(draft[activeEditingField.id] ?? "") : "";

  useEffect(() => {
    if (!editingFieldId) return;
    setTextToolNotice(null);
    const frame = window.requestAnimationFrame(() => {
      const target = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("[data-message-inline-field-id]") ?? [])
        .find((element) => element.dataset.messageInlineFieldId === editingFieldId);
      target?.scrollIntoView({ block: "center", behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [dialogRef, editingFieldId]);

  function clearTextSelection() {
    textSelectionRef.current = null;
  }

  function resetTextEditing() {
    setActiveTextFieldId(null);
    setTextToolNotice(null);
    clearTextSelection();
  }

  function handleTextSelection(field: DashboardFieldDefinition, start: number, end: number) {
    setActiveTextFieldId(field.id);
    textSelectionRef.current = { fieldId: field.id, start, end };
  }

  function restoreTextSelection(fieldId: string, start: number, end: number) {
    if (selectionFrameRef.current !== null) window.cancelAnimationFrame(selectionFrameRef.current);
    selectionFrameRef.current = window.requestAnimationFrame(() => {
      selectionFrameRef.current = window.requestAnimationFrame(() => {
        selectionFrameRef.current = null;
        const saved = textSelectionRef.current;
        if (saved?.fieldId !== fieldId || saved.start !== start || saved.end !== end) return;
        const inline = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-inline-field-id]") ?? [])
          .find((element) => element.dataset.messageInlineFieldId === fieldId);
        const form = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-field-id]") ?? [])
          .find((element) => element.dataset.messageFieldId === fieldId);
        const control = inline ?? form;
        if (!control) return;
        // Formatting must preserve the range without opening a keyboard.
        // A focused control stays focused through the toolbar's pointer guard.
        control.setSelectionRange(start, end);
      });
    });
  }

  function applyTextEdit(result: MessageEditorTextEditResult) {
    if (!activeTextField) return;
    const limitNotice = messageEditorTextLimitNotice(activeTextField, result.value);
    if (limitNotice) {
      setTextToolNotice(limitNotice);
      restoreTextSelection(
        activeTextField.id,
        textSelectionRef.current?.start ?? result.value.length,
        textSelectionRef.current?.end ?? result.value.length,
      );
      return;
    }
    setTextToolNotice(null);
    recordChanges([{ field: activeTextField, raw: result.value }], false);
    textSelectionRef.current = { fieldId: activeTextField.id, start: result.start, end: result.end };
    setSelectedFieldId(activeTextField.id);
    restoreTextSelection(activeTextField.id, result.start, result.end);
  }

  function replaceTextSelection(insertion: string, selectInserted = false) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    applyTextEdit(replaceMessageEditorText(current, insertion, saved, selectInserted));
  }

  function wrapText(prefix: string, suffix: string, placeholder: string) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    applyTextEdit(wrapMessageEditorText(current, prefix, suffix, placeholder, saved));
  }

  function prefixTextLines(prefix: string, placeholder: string) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    applyTextEdit(prefixMessageEditorTextLines(current, prefix, placeholder, saved));
  }

  function insertVariable(key: string) {
    if (!variables || !activeTextField) return;
    const targetId = activeTextField.id;
    const token = formatTemplateVariable(variables.syntax, key);
    const current = String(latestDraftRef.current[targetId] ?? "");
    const saved = textSelectionRef.current?.fieldId === targetId ? textSelectionRef.current : null;
    const separator = !saved && current && !/\s$/.test(current) ? " " : "";
    replaceTextSelection(`${separator}${token}`);
    setView("canvas");
    setEditingFieldId(targetId);
  }

  return {
    activeTextField,
    activeEditingField,
    activeEditingValue,
    activeTextFieldId,
    textToolNotice,
    textSelectionRef,
    setActiveTextFieldId,
    setTextToolNotice,
    clearTextSelection,
    resetTextEditing,
    handleTextSelection,
    replaceTextSelection,
    wrapText,
    prefixTextLines,
    insertVariable,
  };
}
