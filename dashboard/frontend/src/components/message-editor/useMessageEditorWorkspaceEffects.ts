import { useEffect, type Dispatch, type RefObject, type SetStateAction } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import type { MessageEditorView } from "./messageEditorModel";

interface UseMessageEditorWorkspaceEffectsOptions {
  view: MessageEditorView;
  selectedFieldId: string | null;
  visualFields: DashboardFieldDefinition[];
  contextPanelRef: RefObject<HTMLElement>;
  canvasPaneRef: RefObject<HTMLElement>;
  setSelectedFieldId: Dispatch<SetStateAction<string | null>>;
  setContextAnchorFieldId: Dispatch<SetStateAction<string | null>>;
  setContextPlacement(value: null): void;
  setEditingFieldId: Dispatch<SetStateAction<string | null>>;
  setView: Dispatch<SetStateAction<MessageEditorView>>;
  setActiveTextFieldId: Dispatch<SetStateAction<string | null>>;
  clearTextSelection(): void;
}

export function useMessageEditorWorkspaceEffects(options: UseMessageEditorWorkspaceEffectsOptions) {
  const {
    view,
    selectedFieldId,
    visualFields,
    contextPanelRef,
    canvasPaneRef,
    setSelectedFieldId,
    setContextAnchorFieldId,
    setContextPlacement,
    setEditingFieldId,
    setView,
    setActiveTextFieldId,
    clearTextSelection,
  } = options;

  useEffect(() => {
    if (!selectedFieldId || visualFields.some((field) => field.id === selectedFieldId)) return;
    setSelectedFieldId(null);
    setContextAnchorFieldId(null);
    setContextPlacement(null);
    setEditingFieldId(null);
    setView("canvas");
    setActiveTextFieldId(null);
    clearTextSelection();
  }, [selectedFieldId, visualFields]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (view === "canvas") return;
    const frame = window.requestAnimationFrame(() => {
      const panel = contextPanelRef.current;
      const target = panel?.querySelector<HTMLElement>(
        ".osk-message-editor__view-body input:not(:disabled), .osk-message-editor__view-body textarea:not(:disabled), .osk-message-editor__view-body button:not(:disabled), .osk-message-editor__view-body select:not(:disabled)",
      ) ?? panel?.querySelector<HTMLElement>("button:not(:disabled)");
      target?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [view, selectedFieldId]);

  useEffect(() => {
    if (!canvasPaneRef.current) return;
    (canvasPaneRef.current as HTMLElement & { inert: boolean }).inert = view !== "canvas";
  }, [view]);
}
