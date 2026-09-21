import { useEffect } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import type { MessageEditorView } from "./messageEditorModel";

interface Props {
  editorKey: string; fieldId?: string | null; fields: DashboardFieldDefinition[];
  setSelectedFieldId(id: string): void; setContextAnchorFieldId(id: string): void; setView(view: MessageEditorView): void;
}
export function useMessageEditorInitialField({ editorKey, fieldId, fields, setSelectedFieldId, setContextAnchorFieldId, setView }: Props) {
  useEffect(() => {
    if (!fieldId || !fields.some(field => field.id === fieldId)) return;
    setSelectedFieldId(fieldId); setContextAnchorFieldId(fieldId); setView("inspector");
    const frame = window.requestAnimationFrame(() => {
      const wrapper = Array.from(document.querySelectorAll<HTMLElement>(".osk-message-editor [data-field-id]")).find(node => node.dataset.fieldId === fieldId);
      wrapper?.querySelector<HTMLElement>("input:not(:disabled), textarea:not(:disabled), button:not(:disabled)")?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editorKey, fieldId]); // Apenas ao abrir: preserva a navegação durante a edição.
}
