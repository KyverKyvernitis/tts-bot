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
        title={view === "canvas" ? "Aplicar ao rascunho e voltar" : "Voltar à mensagem"}
      >
        <ChevronLeft size={19} />
      </button>
      <div className="osk-message-editor__title">
        <div className="osk-message-editor__title-context"><small>{sectionLabel}</small><span className="osk-message-editor__dirty" data-visible={localDirty || jsonDirty || undefined} aria-live="polite" aria-hidden={!(localDirty || jsonDirty)}>Alterado</span></div>
        <strong title={groupLabel}>{groupLabel}</strong>
      </div>
      <div className="osk-message-editor__header-actions">
        <div className="osk-message-editor__history" role="group" aria-label="Histórico de alterações">
          <button type="button" disabled={historyIndex <= 0 || historyBlocked} onClick={onUndo} aria-label="Desfazer" title="Desfazer"><Undo2 size={17} /></button>
          <button type="button" disabled={historyIndex >= historyLength || historyBlocked} onClick={onRedo} aria-label="Refazer" title="Refazer"><Redo2 size={17} /></button>
        </div>
        {hasVariables ? <button type="button" disabled={pendingJson} onClick={onVariables} aria-label="Abrir variáveis" title="Variáveis" aria-expanded={view === "variables"} data-active={view === "variables" || undefined}><Variable size={17} aria-hidden="true" /><span className="osk-workspace-tool-label">Variáveis</span></button> : null}
        {presentation !== "color_panel" && <button type="button" disabled={pendingJson} onClick={onJson} aria-label="Abrir JSON avançado" title="JSON avançado" aria-expanded={view === "json"} data-active={view === "json" || undefined}><Braces size={17} aria-hidden="true" /><span className="osk-workspace-tool-label">Avançado</span></button>}
      </div>
    </header>
  );
}
