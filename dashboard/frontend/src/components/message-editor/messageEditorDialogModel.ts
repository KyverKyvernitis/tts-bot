import type { MessageEditorView } from "./messageEditorModel";

export type MessageEditorEscapeAction = "stop-editing" | "auxiliary-back" | "apply";

export function messageEditorViewportHeight(
  visualViewportHeight: number | null | undefined,
  innerHeight: number,
  documentHeight: number,
): number {
  return Math.max(1, Math.round(Math.min(
    visualViewportHeight ?? Number.POSITIVE_INFINITY,
    innerHeight,
    documentHeight || Number.POSITIVE_INFINITY,
  )));
}

export function messageEditorEscapeAction(
  editingFieldId: string | null,
  view: MessageEditorView,
): MessageEditorEscapeAction {
  if (editingFieldId) return "stop-editing";
  if (view !== "canvas") return "auxiliary-back";
  return "apply";
}
