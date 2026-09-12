import { useCallback, useEffect, useRef, useState, type TransitionEvent } from "react";

type CloseIntent = "apply" | "discard";

interface UseMessageEditorDialogCloseOptions {
  editorKey: string;
  localDirty: boolean;
  jsonDirty: boolean;
  onApply(): void;
  onDiscard(): void;
}

export function useMessageEditorDialogClose({
  editorKey,
  localDirty,
  jsonDirty,
  onApply,
  onDiscard,
}: UseMessageEditorDialogCloseOptions) {
  const [visible, setVisible] = useState(false);
  const closeIntentRef = useRef<CloseIntent | null>(null);
  const finalIntent = useRef<CloseIntent | null>(null);
  const closing = useRef(false);
  const closeTimerRef = useRef<number | null>(null);
  const historyMarker = useRef(`osk-editor-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const localDirtyRef = useRef(localDirty);
  const jsonDirtyRef = useRef(jsonDirty);
  const onApplyRef = useRef(onApply);
  const onDiscardRef = useRef(onDiscard);
  const applyActionRef = useRef<() => void>(() => undefined);
  const auxiliaryBackRef = useRef<() => void>(() => undefined);

  localDirtyRef.current = localDirty;
  jsonDirtyRef.current = jsonDirty;
  onApplyRef.current = onApply;
  onDiscardRef.current = onDiscard;

  const restoreHistoryMarker = useCallback(() => {
    if (window.history.state?.oskMessageEditor === historyMarker.current) return;
    window.history.pushState(
      { ...(window.history.state || {}), oskMessageEditor: historyMarker.current },
      "",
      window.location.href,
    );
  }, []);

  const finalizeClose = useCallback(() => {
    if (!closing.current) return;
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    const intent = finalIntent.current ?? "apply";
    finalIntent.current = null;
    if (intent === "apply") onApplyRef.current();
    else onDiscardRef.current();
  }, []);

  const beginClose = useCallback((intent: CloseIntent) => {
    if (closing.current) return;
    if (intent === "discard" && (localDirtyRef.current || jsonDirtyRef.current)
      && !window.confirm("Descartar as alterações feitas neste editor?")) {
      restoreHistoryMarker();
      return;
    }
    closing.current = true;
    finalIntent.current = intent;
    setVisible(false);
    closeTimerRef.current = window.setTimeout(finalizeClose, 300);
  }, [finalizeClose, restoreHistoryMarker]);

  const handleHistoryClose = useCallback(() => {
    const intent = closeIntentRef.current;
    closeIntentRef.current = null;
    if (intent) beginClose(intent);
    else applyActionRef.current();
  }, [beginClose]);

  const requestClose = useCallback((intent: CloseIntent) => {
    if (closing.current) return;
    closeIntentRef.current = intent;
    if (window.history.state?.oskMessageEditor === historyMarker.current) window.history.back();
    else handleHistoryClose();
  }, [handleHistoryClose]);

  useEffect(() => {
    closing.current = false;
    closeIntentRef.current = null;
    finalIntent.current = null;
    setVisible(false);
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => setVisible(true));
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [editorKey]);

  function handleTransitionEnd(event: TransitionEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget || visible || !closing.current) return;
    if (event.propertyName === "opacity") finalizeClose();
  }

  return {
    visible,
    requestClose,
    restoreHistoryMarker,
    closeIntentRef,
    applyActionRef,
    auxiliaryBackRef,
    handleHistoryClose,
    handleTransitionEnd,
  };
}
