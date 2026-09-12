import { useEffect, useRef, type MutableRefObject } from "react";
import type { MessageEditorView } from "./messageEditorModel";
import { messageEditorEscapeAction, messageEditorViewportHeight } from "./messageEditorDialogModel";

interface MutableRefLike<T> {
  current: T | null;
}

interface UseMessageEditorDialogEnvironmentOptions {
  editorKey: string;
  dialogRef: MutableRefLike<HTMLDivElement>;
  view: MessageEditorView;
  editingFieldId: string | null;
  onUndo(): void;
  onRedo(): void;
  onStopEditing(): void;
  closeIntentRef: MutableRefObject<"apply" | "discard" | null>;
  applyActionRef: MutableRefObject<() => void>;
  auxiliaryBackRef: MutableRefObject<() => void>;
  handleHistoryClose(): void;
  restoreHistoryMarker(): void;
}

export function useMessageEditorDialogEnvironment({
  editorKey,
  dialogRef,
  view,
  editingFieldId,
  onUndo,
  onRedo,
  onStopEditing,
  closeIntentRef,
  applyActionRef,
  auxiliaryBackRef,
  handleHistoryClose,
  restoreHistoryMarker,
}: UseMessageEditorDialogEnvironmentOptions) {
  const returnFocusRef = useRef<HTMLElement | null>(null) as MutableRefObject<HTMLElement | null>;
  const scrollPositionRef = useRef(0);
  const viewRef = useRef<MessageEditorView>(view);
  const editingFieldIdRef = useRef(editingFieldId);
  const undoRef = useRef(onUndo);
  const redoRef = useRef(onRedo);
  const stopEditingRef = useRef(onStopEditing);

  viewRef.current = view;
  editingFieldIdRef.current = editingFieldId;
  undoRef.current = onUndo;
  redoRef.current = onRedo;
  stopEditingRef.current = onStopEditing;

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousPosition = document.body.style.position;
    const previousTop = document.body.style.top;
    const previousWidth = document.body.style.width;
    scrollPositionRef.current = window.scrollY;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    document.body.style.position = "fixed";
    document.body.style.top = `-${scrollPositionRef.current}px`;
    document.body.style.width = "100%";

    const syncVisualViewport = () => {
      const height = messageEditorViewportHeight(
        window.visualViewport?.height,
        window.innerHeight,
        document.documentElement.clientHeight,
      );
      dialogRef.current?.style.setProperty("--osk-message-editor-viewport-height", `${height}px`);
    };
    syncVisualViewport();
    window.visualViewport?.addEventListener("resize", syncVisualViewport);
    window.visualViewport?.addEventListener("scroll", syncVisualViewport);
    window.addEventListener("resize", syncVisualViewport);

    restoreHistoryMarker();
    window.setTimeout(() => dialogRef.current?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)")?.focus(), 0);
    const closeOpenContextSelect = () => {
      const trigger = dialogRef.current?.querySelector<HTMLButtonElement>(
        ".osk-message-context-select[data-open] .osk-message-context-select__trigger",
      );
      if (!trigger) return false;
      trigger.click();
      return true;
    };
    const onBackRequest = () => {
      if (closeIntentRef.current) {
        handleHistoryClose();
        return;
      }
      if (closeOpenContextSelect()) {
        restoreHistoryMarker();
        return;
      }
      if (viewRef.current !== "canvas") {
        auxiliaryBackRef.current();
        restoreHistoryMarker();
        return;
      }
      handleHistoryClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) redoRef.current(); else undoRef.current();
        return;
      }
      if (event.key === "Escape") {
        if (event.defaultPrevented) return;
        if (closeOpenContextSelect()) {
          event.preventDefault();
          return;
        }
        event.preventDefault();
        const action = messageEditorEscapeAction(editingFieldIdRef.current, viewRef.current);
        if (action === "stop-editing") stopEditingRef.current();
        else if (action === "auxiliary-back") auxiliaryBackRef.current();
        else applyActionRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.offsetParent !== null && !element.closest("[inert]"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("osk:message-editor-back", onBackRequest as EventListener);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.body.style.position = previousPosition;
      document.body.style.top = previousTop;
      document.body.style.width = previousWidth;
      window.scrollTo({ top: scrollPositionRef.current, behavior: "auto" });
      window.removeEventListener("osk:message-editor-back", onBackRequest as EventListener);
      window.removeEventListener("keydown", onKeyDown);
      window.visualViewport?.removeEventListener("resize", syncVisualViewport);
      window.visualViewport?.removeEventListener("scroll", syncVisualViewport);
      window.removeEventListener("resize", syncVisualViewport);
      window.setTimeout(() => returnFocusRef.current?.focus(), 0);
    };
  }, [
    applyActionRef,
    auxiliaryBackRef,
    closeIntentRef,
    dialogRef,
    editorKey,
    handleHistoryClose,
    restoreHistoryMarker,
  ]);
}
