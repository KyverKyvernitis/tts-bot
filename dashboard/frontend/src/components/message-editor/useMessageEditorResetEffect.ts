import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { MessageEditorContextPlacement } from "./messageEditorModel";

interface MessageEditorResetEffectOptions {
  editorKey: string;
  draft: Record<string, unknown>;
  resetViewState(): void;
  setContextPlacement: Dispatch<SetStateAction<MessageEditorContextPlacement | null>>;
  resetJson(draft: Record<string, unknown>): void;
  resetTextEditing(): void;
  resetHistory(draft: Record<string, unknown>): void;
}

export function useMessageEditorResetEffect(options: MessageEditorResetEffectOptions): void {
  const {
    editorKey,
    draft,
    resetViewState,
    setContextPlacement,
    resetJson,
    resetTextEditing,
    resetHistory,
  } = options;

  useEffect(() => {
    resetViewState();
    setContextPlacement(null);
    resetJson(draft);
    resetTextEditing();
    resetHistory(draft);
  }, [editorKey]); // eslint-disable-line react-hooks/exhaustive-deps
}
