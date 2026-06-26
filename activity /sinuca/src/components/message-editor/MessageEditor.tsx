import { ChevronLeft, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { MessageJsonEditor } from "./MessageJsonEditor";
import { MessagePreview } from "./MessagePreview";
import { MessageVariablesPanel } from "./MessageVariablesPanel";
import { MessageVisualEditor } from "./MessageVisualEditor";
import type {
  JsonFieldChange,
  MessageEditorMobileView,
  MessageEditorMode,
  MessageEditorProps,
} from "./messageEditorTypes";
import {
  messageFieldsObject,
  parseMessageJson,
  pendingChangesReachedDraft,
  serializeMessageFields,
} from "./messageEditorUtils";

export function MessageEditor({
  sectionId,
  sectionLabel,
  groupLabel,
  fields,
  values,
  draft,
  guildOptions,
  variables,
  hasUnsavedChanges,
  applying,
  onChange,
  onApply,
  onBack,
}: MessageEditorProps) {
  const editorKey = `${sectionId}:${groupLabel}`;
  const serializedDraft = useMemo(
    () => serializeMessageFields(fields, draft),
    [draft, fields],
  );
  const [mode, setMode] = useState<MessageEditorMode>("visual");
  const [mobileView, setMobileView] = useState<MessageEditorMobileView>("edit");
  const [jsonText, setJsonText] = useState(serializedDraft);
  const [jsonBaseline, setJsonBaseline] = useState<Record<string, unknown>>(() => messageFieldsObject(fields, draft));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingJsonChanges, setPendingJsonChanges] = useState<JsonFieldChange[] | null>(null);

  useEffect(() => {
    setMode("visual");
    setMobileView("edit");
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(fields, draft));
    setJsonDirty(false);
    setJsonError(null);
    setPendingJsonChanges(null);
  }, [editorKey]); // serializedDraft intentionally excluded: this reset is scoped to navigation.

  useEffect(() => {
    if (!jsonDirty && pendingJsonChanges === null) {
      setJsonText(serializedDraft);
      setJsonBaseline(messageFieldsObject(fields, draft));
    }
  }, [jsonDirty, pendingJsonChanges, serializedDraft]);

  useEffect(() => {
    if (!pendingJsonChanges || !pendingChangesReachedDraft(pendingJsonChanges, draft)) return;
    setPendingJsonChanges(null);
    setJsonDirty(false);
    setJsonError(null);
    setJsonText(serializeMessageFields(fields, draft));
    setJsonBaseline(messageFieldsObject(fields, draft));
    void onApply();
  }, [draft, fields, onApply, pendingJsonChanges]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const media = window.matchMedia("(max-width: 899px)");
    const previousOverflow = document.body.style.overflow;

    const syncBodyLock = () => {
      document.body.style.overflow = media.matches ? "hidden" : previousOverflow;
    };

    syncBodyLock();
    media.addEventListener("change", syncBodyLock);
    return () => {
      media.removeEventListener("change", syncBodyLock);
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  function handleJsonChange(next: string) {
    setJsonText(next);
    setJsonDirty(true);
    setJsonError(null);
  }

  function handleApply() {
    if (applying || pendingJsonChanges) return;

    if (jsonDirty) {
      try {
        const changes = parseMessageJson(jsonText, fields, draft, jsonBaseline);
        setJsonError(null);
        if (changes.length === 0) {
          setJsonDirty(false);
          setJsonText(serializedDraft);
          setJsonBaseline(messageFieldsObject(fields, draft));
          if (hasUnsavedChanges) void onApply();
          return;
        }
        setPendingJsonChanges(changes);
        for (const change of changes) {
          onChange(change.field, change.raw);
        }
      } catch (error) {
        setJsonError(error instanceof Error ? error.message : "JSON inválido.");
        setMode("json");
        setMobileView("edit");
      }
      return;
    }

    if (hasUnsavedChanges) void onApply();
  }

  const applyDisabled = applying || Boolean(pendingJsonChanges) || (!hasUnsavedChanges && !jsonDirty);

  return (
    <div className="osk-message-editor" data-mobile-view={mobileView}>
      <div className="osk-message-editor__shell">
        <header className="osk-message-editor__header">
          <button type="button" className="osk-message-editor__back" onClick={onBack}>
            <ChevronLeft size={18} />
            <span>Grupos</span>
          </button>
          <div className="osk-message-editor__title">
            <small>{sectionLabel}</small>
            <strong>{groupLabel}</strong>
          </div>
        </header>

        <nav className="osk-message-editor__primary-tabs" aria-label="Editor de mensagem">
          <button
            type="button"
            data-active={mobileView === "edit"}
            onClick={() => setMobileView("edit")}
          >
            Editar
          </button>
          <button
            type="button"
            className="osk-message-editor__preview-tab"
            data-active={mobileView === "preview"}
            onClick={() => setMobileView("preview")}
          >
            Prévia
          </button>
          <button
            type="button"
            className="osk-message-editor__apply"
            disabled={applyDisabled}
            onClick={handleApply}
          >
            {applying || pendingJsonChanges ? <LoaderCircle size={15} className="osk-spin" /> : null}
            Aplicar
          </button>
        </nav>

        <div className="osk-message-editor__workspace">
          <section className="osk-message-editor__edit-pane">
            <div className="osk-message-editor__modes" role="tablist" aria-label="Modo de edição">
              {(["visual", "json", "variables"] as const).map((item) => (
                <button
                  key={item}
                  type="button"
                  role="tab"
                  aria-selected={mode === item}
                  data-active={mode === item}
                  onClick={() => setMode(item)}
                >
                  {item === "visual" ? "Visual" : item === "json" ? "JSON" : "Variáveis"}
                </button>
              ))}
            </div>

            <div className="osk-message-editor__mode-content">
              {mode === "visual" ? (
                <MessageVisualEditor
                  fields={fields}
                  values={values}
                  draft={draft}
                  guildOptions={guildOptions}
                  onChange={onChange}
                />
              ) : mode === "json" ? (
                <MessageJsonEditor
                  value={jsonText}
                  error={jsonError}
                  dirty={jsonDirty}
                  onChange={handleJsonChange}
                />
              ) : (
                <MessageVariablesPanel variables={variables} />
              )}
            </div>
          </section>

          <aside className="osk-message-editor__preview-pane">
            <div className="osk-message-editor__preview-label">
              <strong>Prévia</strong>
              <small>Representação aproximada da mensagem</small>
            </div>
            <MessagePreview groupLabel={groupLabel} fields={fields} draft={draft} />
          </aside>
        </div>
      </div>
    </div>
  );
}
