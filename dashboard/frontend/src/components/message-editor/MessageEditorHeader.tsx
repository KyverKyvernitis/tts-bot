import { Braces, ChevronLeft, Redo2, Undo2, Variable } from "lucide-react";
import type { DashboardMessageEditorPresentation } from "../../types/dashboard";
import type { MessageEditorView } from "./messageEditorModel";

export interface MessageEditorHeaderProps {
  view: MessageEditorView;
  sectionLabel: string;
  groupLabel: string;
  historyIndex: number;
  historyLength: number;
  jsonDirty: boolean;
  pendingJson: boolean;
  localDirty: boolean;
  hasVariables: boolean;
  presentation: DashboardMessageEditorPresentation;
  onBack(): void;
  onUndo(): void;
  onRedo(): void;
  onVariables(): void;
  onJson(): void;
}

export function MessageEditorHeader({
  view,
  sectionLabel,
  groupLabel,
  historyIndex,
  historyLength,
  jsonDirty,
  pendingJson,
  localDirty,
  hasVariables,
  presentation,
  onBack,
  onUndo,
  onRedo,
  onVariables,
  onJson,
}: MessageEditorHeaderProps) {
  const historyBlocked = jsonDirty || pendingJson;
  return (
    <header className="osk-message-editor__header">
      <button
        type="button"
        className="osk-message-editor__back"
        onClick={onBack}
        aria-label={view === "canvas" ? "Aplicar ao rascunho e voltar" : "Voltar à mensagem"}
      >
        <ChevronLeft size={19} />
      </button>
      <div className="osk-message-editor__title">
        <small>{sectionLabel}</small>
        <strong>{groupLabel}</strong>
      </div>
      <div className="osk-message-editor__header-actions">
        <button type="button" disabled={historyIndex <= 0 || historyBlocked} onClick={onUndo} aria-label="Desfazer" title="Desfazer"><Undo2 size={17} /></button>
        <button type="button" disabled={historyIndex >= historyLength || historyBlocked} onClick={onRedo} aria-label="Refazer" title="Refazer"><Redo2 size={17} /></button>
        {hasVariables ? <button type="button" disabled={pendingJson} onClick={onVariables} aria-label="Abrir variáveis" title="Variáveis"><Variable size={17} /></button> : null}
        {presentation !== "color_panel" && <button type="button" disabled={pendingJson} onClick={onJson} aria-label="Abrir JSON avançado" title="JSON avançado"><Braces size={17} /></button>}
        <span className="osk-message-editor__dirty" data-visible={localDirty || jsonDirty || undefined} aria-live="polite" aria-hidden={!(localDirty || jsonDirty)}>Alterado</span>
      </div>
    </header>
  );
}
