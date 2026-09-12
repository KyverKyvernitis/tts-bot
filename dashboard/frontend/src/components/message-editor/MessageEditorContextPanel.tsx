import { X } from "lucide-react";
import type { CSSProperties, RefObject } from "react";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardTemplateVariables,
} from "../../types/dashboard";
import { MessageJsonEditor } from "./MessageJsonEditor";
import { MessageVariablesPanel } from "./MessageVariablesPanel";
import { MessageVisualEditor } from "./MessageVisualEditor";
import type { MessageEditorContextPlacement, MessageEditorView } from "./messageEditorModel";

export interface MessageEditorContextPanelProps {
  view: MessageEditorView;
  anchorFieldId: string | null;
  panelRef: RefObject<HTMLElement>;
  placement: MessageEditorContextPlacement | null;
  title: string;
  description: string;
  pendingJson: boolean;
  onClose(): void;
  variables?: DashboardTemplateVariables;
  activeTextFieldLabel?: string;
  onInsertVariable?(key: string): void;
  jsonText: string;
  jsonError: string | null;
  jsonDirty: boolean;
  onJsonChange(value: string): void;
  onJsonApply(): void;
  onJsonDiscard(): void;
  selectedField: DashboardFieldDefinition | null;
  senderSelected: boolean;
  senderEnabled: boolean;
  inspectorFields: DashboardFieldDefinition[];
  baseline: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  selectedFieldId: string | null;
  selectedColorSlot: number | null;
  colorSlotIds: number[] | null;
  onColorSlotSelect(slotNumber: number): void;
  onFocusField(field: DashboardFieldDefinition): void;
  onTextSelection(field: DashboardFieldDefinition, start: number, end: number): void;
  onFieldChange(field: DashboardFieldDefinition, raw: unknown): void;
}

export function MessageEditorContextPanel(props: MessageEditorContextPanelProps) {
  const {
    view,
    anchorFieldId,
    panelRef,
    placement,
    title,
    description,
    pendingJson,
    onClose,
    variables,
    activeTextFieldLabel,
    onInsertVariable,
    jsonText,
    jsonError,
    jsonDirty,
    onJsonChange,
    onJsonApply,
    onJsonDiscard,
    selectedField,
    senderSelected,
    senderEnabled,
    inspectorFields,
    baseline,
    draft,
    guildOptions,
    selectedFieldId,
    selectedColorSlot,
    colorSlotIds,
    onColorSlotSelect,
    onFocusField,
    onTextSelection,
    onFieldChange,
  } = props;

  const contextStyle = placement ? ({
    "--osk-message-context-left": `${placement.left}px`,
    "--osk-message-context-top": `${placement.top}px`,
    "--osk-message-context-width": `${placement.width}px`,
  } as CSSProperties) : undefined;

  return (
    <div
      className="osk-message-editor__context-layer"
      data-view={view}
      data-anchored={view === "inspector" && anchorFieldId ? true : undefined}
    >
      <button
        type="button"
        className="osk-message-editor__context-backdrop"
        onClick={onClose}
        disabled={pendingJson}
        aria-label="Fechar configurações"
      />
      <section
        ref={panelRef}
        className="osk-message-editor__context-popover osk-message-editor__editor-view"
        data-side={placement?.side}
        style={contextStyle}
        role="dialog"
        aria-label={title}
      >
        <div className="osk-message-editor__view-head">
          <div>
            <strong>{title}</strong>
            <small>{description}</small>
          </div>
          <button type="button" onClick={onClose} disabled={pendingJson} aria-label="Fechar"><X size={17} /></button>
        </div>
        <div className="osk-message-editor__view-body">
          {view === "variables" ? (
            <MessageVariablesPanel variables={variables} insertTargetLabel={activeTextFieldLabel} onInsert={onInsertVariable} />
          ) : view === "json" ? (
            <MessageJsonEditor
              value={jsonText}
              error={jsonError}
              dirty={jsonDirty}
              applying={pendingJson}
              onChange={onJsonChange}
              onApply={onJsonApply}
              onDiscard={onJsonDiscard}
            />
          ) : selectedField ? (
            <>
              {senderSelected && <div className="osk-message-sender-note" data-enabled={senderEnabled || undefined}>
                <strong>{senderEnabled ? "Webhook ativado" : "Enviado pela Osaka"}</strong>
                <span>{senderEnabled ? "A identidade abaixo será usada apenas nesta mensagem." : "Ative o webhook para escolher outro nome e avatar."}</span>
              </div>}
              <MessageVisualEditor
                fields={inspectorFields}
                baseline={baseline}
                draft={draft}
                guildOptions={guildOptions}
                onChange={onFieldChange}
                selectedFieldId={selectedFieldId}
                selectedColorSlot={selectedColorSlot}
                colorSlotIds={colorSlotIds}
                onColorSlotSelect={onColorSlotSelect}
                onFocusField={onFocusField}
                onTextSelection={onTextSelection}
                contextual
              />
            </>
          ) : (
            <div className="osk-message-empty">Selecione um elemento da mensagem para abrir suas propriedades.</div>
          )}
        </div>
      </section>
    </div>
  );
}
