import type { TransitionEvent } from "react";
import type { MessageEditorView } from "./messageEditorModel";
import { useMessageEditorDialogClose } from "./useMessageEditorDialogClose";
import { useMessageEditorDialogEnvironment } from "./useMessageEditorDialogEnvironment";

interface MutableRefLike<T> {
  current: T | null;
}

interface UseMessageEditorDialogLifecycleOptions {
  editorKey: string;
  dialogRef: MutableRefLike<HTMLDivElement>;
  view: MessageEditorView;
  editingFieldId: string | null;
  localDirty: boolean;
  jsonDirty: boolean;
  onApply(): void;
  onDiscard(): void;
  onUndo(): void;
  onRedo(): void;
  onStopEditing(): void;
}

export function useMessageEditorDialogLifecycle(options: UseMessageEditorDialogLifecycleOptions) {
  const close = useMessageEditorDialogClose(options);
  useMessageEditorDialogEnvironment({
    editorKey: options.editorKey,
    dialogRef: options.dialogRef,
    view: options.view,
    editingFieldId: options.editingFieldId,
    onUndo: options.onUndo,
    onRedo: options.onRedo,
    onStopEditing: options.onStopEditing,
    closeIntentRef: close.closeIntentRef,
    applyActionRef: close.applyActionRef,
    auxiliaryBackRef: close.auxiliaryBackRef,
    handleHistoryClose: close.handleHistoryClose,
    restoreHistoryMarker: close.restoreHistoryMarker,
  });

  return {
    visible: close.visible,
    requestClose: close.requestClose,
    restoreHistoryMarker: close.restoreHistoryMarker,
    applyActionRef: close.applyActionRef,
    auxiliaryBackRef: close.auxiliaryBackRef,
    handleTransitionEnd: close.handleTransitionEnd as (event: TransitionEvent<HTMLDivElement>) => void,
  };
}
