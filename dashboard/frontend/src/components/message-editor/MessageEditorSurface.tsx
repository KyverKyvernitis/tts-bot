import type { RefObject, TransitionEventHandler } from "react";
import type { MessageEditorCanvasProps } from "./MessageEditorCanvas";
import { MessageEditorCanvas } from "./MessageEditorCanvas";
import type { MessageEditorContextPanelProps } from "./MessageEditorContextPanel";
import { MessageEditorContextPanel } from "./MessageEditorContextPanel";
import type { MessageEditorHeaderProps } from "./MessageEditorHeader";
import { MessageEditorHeader } from "./MessageEditorHeader";
import type { MessageEditorTextDockProps } from "./MessageEditorTextDock";
import { MessageEditorTextDock } from "./MessageEditorTextDock";
import type { MessageEditorView } from "./messageEditorModel";

export interface MessageEditorSurfaceProps {
  dialogRef: RefObject<HTMLDivElement>;
  workspaceRef: RefObject<HTMLDivElement>;
  visible: boolean;
  view: MessageEditorView;
  textEditing: boolean;
  groupLabel: string;
  onTransitionEnd: TransitionEventHandler<HTMLDivElement>;
  header: MessageEditorHeaderProps;
  canvas: MessageEditorCanvasProps;
  context: MessageEditorContextPanelProps | null;
  textDock: MessageEditorTextDockProps | null;
  pendingJson: boolean;
  dirty: boolean;
  onDiscard(): void;
  onApply(): void;
}

export function MessageEditorSurface({
  dialogRef,
  workspaceRef,
  visible,
  view,
  textEditing,
  groupLabel,
  onTransitionEnd,
  header,
  canvas,
  context,
  textDock,
  pendingJson,
  dirty,
  onDiscard,
  onApply,
}: MessageEditorSurfaceProps) {
  return (
    <div
      ref={dialogRef}
      className="osk-root osk-message-editor osk-message-editor--unified"
      data-visible={visible || undefined}
      data-editor-view={view}
      data-text-editing={textEditing ? "true" : undefined}
      role="dialog"
      aria-modal="true"
      aria-label={`Editar ${groupLabel}`}
      onTransitionEnd={onTransitionEnd}
    >
      <div className="osk-message-editor__shell">
        <MessageEditorHeader {...header} />
        <div ref={workspaceRef} className="osk-message-editor__workspace" data-context-open={view !== "canvas" || undefined}>
          <MessageEditorCanvas {...canvas} />
          {context ? <MessageEditorContextPanel {...context} /> : null}
        </div>
        {textDock ? <MessageEditorTextDock {...textDock} /> : null}
        <footer className="osk-message-editor__footer" data-text-editing={textEditing ? "true" : undefined}>
          <button type="button" className="osk-secondary-button" onClick={onDiscard}>Descartar alterações</button>
          <button type="button" className="osk-primary-button" disabled={pendingJson} onClick={onApply}>
            {pendingJson ? "Aplicando..." : dirty ? "Concluir edição" : "Concluir"}
          </button>
        </footer>
        <small className="osk-editor-draft-note">Concluir mantém a edição no rascunho. Salve no servidor ao voltar ao módulo.</small>
      </div>
    </div>
  );
}
