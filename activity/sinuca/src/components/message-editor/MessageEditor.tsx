import {
  Braces,
  ChevronLeft,
  Eye,
  Image,
  LayoutPanelTop,
  ListPlus,
  MessageSquareText,
  Variable,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  formatTemplateVariable,
  messageFieldsObject,
  parseMessageJson,
  pendingChangesReachedDraft,
  serializeMessageFields,
} from "./messageEditorUtils";

const MODE_LABELS: Record<MessageEditorMode, string> = {
  content: "Conteúdo",
  appearance: "Aparência",
  components: "Componentes",
  variables: "Variáveis",
  json: "Avançado",
};

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

function fieldMode(fieldId: string, label: string, type: string): Exclude<MessageEditorMode, "variables" | "json"> {
  const text = `${fieldId} ${label}`.toLocaleLowerCase("pt-BR");
  if (/(button|botão|placeholder|seletor|select|component|style|estilo)/.test(text)) return "components";
  if (type === "color" || type === "url" || /(image|imagem|media|mídia|banner|avatar|author|autor|thumbnail|ícone|icon|emoji|cor)/.test(text)) return "appearance";
  return "content";
}


function editorVisualFieldVisible(editorId: string, fieldId: string, draft: Record<string, unknown>): boolean {
  if (editorId === "welcome-public") {
    const renderMode = String(draft["welcome.render_mode"] || "components_v2");
    if (fieldId.includes(".embed.")) {
      if (renderMode !== "embed") return false;
      if (fieldId === "welcome.embed.color") return String(draft["welcome.embed.color_mode"] || "fixed") === "fixed";
      if (fieldId === "welcome.embed.author_icon_url") return String(draft["welcome.embed.author_icon_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.thumbnail_url") return String(draft["welcome.embed.thumbnail_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.image_url") return String(draft["welcome.embed.image_mode"] || "none") === "custom";
      if (fieldId === "welcome.embed.footer_icon_url") return String(draft["welcome.embed.footer_icon_mode"] || "none") === "custom";
      return true;
    }
    if (fieldId.includes(".public.")) return renderMode !== "embed";
  }
  return true;
}

function modeIcon(mode: MessageEditorMode) {
  if (mode === "content") return MessageSquareText;
  if (mode === "appearance") return Image;
  if (mode === "components") return ListPlus;
  if (mode === "variables") return Variable;
  return Braces;
}

export function MessageEditor(props: MessageEditorProps) {
  const {
    editorId,
    sectionLabel,
    groupLabel,
    description,
    fields,
    baseline,
    draft,
    guildOptions,
    botName,
    botAvatarUrl,
    variables,
    onChange,
    onApply,
    onDiscard,
  } = props;

  const editorKey = `${props.sectionId}:${editorId}`;
  const serializedDraft = useMemo(() => serializeMessageFields(fields, draft), [draft, fields]);
  const visualFields = useMemo(
    () => fields.filter((field) => editorVisualFieldVisible(editorId, field.id, draft)),
    [draft, editorId, fields],
  );
  const categorizedFields = useMemo(() => ({
    content: visualFields.filter((field) => fieldMode(field.id, field.label, field.type) === "content"),
    appearance: visualFields.filter((field) => fieldMode(field.id, field.label, field.type) === "appearance"),
    components: visualFields.filter((field) => fieldMode(field.id, field.label, field.type) === "components"),
  }), [visualFields]);
  const availableModes = useMemo(() => {
    const modes: MessageEditorMode[] = [];
    if (categorizedFields.content.length) modes.push("content");
    if (categorizedFields.appearance.length) modes.push("appearance");
    if (categorizedFields.components.length) modes.push("components");
    if (variables?.items.length) modes.push("variables");
    modes.push("json");
    return modes;
  }, [categorizedFields, variables]);

  const [mode, setMode] = useState<MessageEditorMode>(availableModes[0] ?? "content");
  const [mobileView, setMobileView] = useState<MessageEditorMobileView>("edit");
  const [jsonText, setJsonText] = useState(serializedDraft);
  const [jsonBaseline, setJsonBaseline] = useState<Record<string, unknown>>(() => messageFieldsObject(fields, draft));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingJsonChanges, setPendingJsonChanges] = useState<JsonFieldChange[] | null>(null);
  const [activeTextFieldId, setActiveTextFieldId] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const closeIntent = useRef<"apply" | "discard" | null>(null);
  const closing = useRef(false);
  const historyMarker = useRef(`osk-editor-${Date.now()}-${Math.random().toString(36).slice(2)}`);

  const localDirty = useMemo(
    () => fields.some((field) => !valuesEqual(baseline[field.id], draft[field.id])),
    [baseline, draft, fields],
  );
  const localDirtyRef = useRef(localDirty);
  const jsonDirtyRef = useRef(jsonDirty);
  const onApplyRef = useRef(onApply);
  const onDiscardRef = useRef(onDiscard);

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

  const handleHistoryClose = useCallback(() => {
    if (closing.current) return;
    const intent = closeIntent.current ?? "discard";
    closeIntent.current = null;
    if (intent === "discard" && (localDirtyRef.current || jsonDirtyRef.current)
      && !window.confirm("Descartar as alterações feitas neste editor?")) {
      restoreHistoryMarker();
      return;
    }
    closing.current = true;
    if (intent === "apply") onApplyRef.current();
    else onDiscardRef.current();
  }, [restoreHistoryMarker]);

  const requestClose = useCallback((intent: "apply" | "discard") => {
    if (closing.current) return;
    closeIntent.current = intent;
    if (window.history.state?.oskMessageEditor === historyMarker.current) window.history.back();
    else handleHistoryClose();
  }, [handleHistoryClose]);

  useEffect(() => {
    setMode(availableModes[0] ?? "content");
    setMobileView("edit");
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(fields, draft));
    setJsonDirty(false);
    setJsonError(null);
    setPendingJsonChanges(null);
    setActiveTextFieldId(visualFields.find((field) => (field.type === "text" || field.type === "textarea") && fieldMode(field.id, field.label, field.type) === "content")?.id
      ?? visualFields.find((field) => field.type === "text" || field.type === "textarea")?.id
      ?? null);
    closing.current = false;
    closeIntent.current = null;
  }, [editorKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!availableModes.includes(mode)) setMode(availableModes[0] ?? "json");
  }, [availableModes, mode]);

  useEffect(() => {
    if (!jsonDirty && pendingJsonChanges === null) {
      setJsonText(serializedDraft);
      setJsonBaseline(messageFieldsObject(fields, draft));
    }
  }, [draft, fields, jsonDirty, pendingJsonChanges, serializedDraft]);

  useEffect(() => {
    if (!pendingJsonChanges || !pendingChangesReachedDraft(pendingJsonChanges, draft)) return;
    setPendingJsonChanges(null);
    setJsonDirty(false);
    setJsonError(null);
    setJsonText(serializeMessageFields(fields, draft));
    setJsonBaseline(messageFieldsObject(fields, draft));
    requestClose("apply");
  }, [draft, fields, pendingJsonChanges]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    restoreHistoryMarker();
    window.setTimeout(() => dialogRef.current?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)")?.focus(), 0);
    const onBackRequest = () => handleHistoryClose();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose("discard");
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.offsetParent !== null);
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
      window.removeEventListener("osk:message-editor-back", onBackRequest as EventListener);
      window.removeEventListener("keydown", onKeyDown);
      window.setTimeout(() => returnFocusRef.current?.focus(), 0);
    };
  }, [editorKey, handleHistoryClose, requestClose, restoreHistoryMarker]);

  function handleJsonChange(next: string) {
    setJsonText(next);
    setJsonDirty(true);
    setJsonError(null);
  }

  function handleApply() {
    if (pendingJsonChanges) return;
    if (jsonDirty) {
      try {
        const changes = parseMessageJson(jsonText, fields, draft, jsonBaseline);
        setJsonError(null);
        if (changes.length === 0) {
          setJsonDirty(false);
          setJsonText(serializedDraft);
          setJsonBaseline(messageFieldsObject(fields, draft));
          requestClose("apply");
          return;
        }
        setPendingJsonChanges(changes);
        for (const change of changes) onChange(change.field, change.raw);
      } catch (error) {
        setJsonError(error instanceof Error ? error.message : "JSON inválido.");
        setMode("json");
        setMobileView("edit");
      }
      return;
    }
    requestClose("apply");
  }

  const activeTextField = fields.find((field) => field.id === activeTextFieldId && (field.type === "text" || field.type === "textarea"))
    ?? categorizedFields.content.find((field) => field.type === "text" || field.type === "textarea")
    ?? null;

  function handleInsertVariable(key: string) {
    if (!variables || !activeTextField) return;
    const token = formatTemplateVariable(variables.syntax, key);
    const current = String(draft[activeTextField.id] ?? "");
    const separator = current && !/\s$/.test(current) ? " " : "";
    onChange(activeTextField, `${current}${separator}${token}`);
    setActiveTextFieldId(activeTextField.id);
    setMode(fieldMode(activeTextField.id, activeTextField.label, activeTextField.type));
    setMobileView("edit");
  }

  const currentFields = mode === "content" || mode === "appearance" || mode === "components" ? categorizedFields[mode] : [];
  const applyDisabled = Boolean(pendingJsonChanges) || (!localDirty && !jsonDirty);

  const editor = <div ref={dialogRef} className="osk-root osk-message-editor" data-mobile-view={mobileView} role="dialog" aria-modal="true" aria-label={`Editar ${groupLabel}`}>
    <div className="osk-message-editor__shell">
      <header className="osk-message-editor__header">
        <button type="button" className="osk-message-editor__back" onClick={() => requestClose("discard")}>
          <ChevronLeft size={18} />
          <span>{sectionLabel}</span>
        </button>
        <div className="osk-message-editor__title">
          <small>{sectionLabel}</small>
          <strong>{groupLabel}</strong>
          {description && <p>{description}</p>}
        </div>
        <span className="osk-message-editor__dirty" data-visible={localDirty || jsonDirty || undefined}>Alterado</span>
      </header>

      <nav className="osk-message-editor__mobile-tabs" aria-label="Visualização do editor">
        <button type="button" data-active={mobileView === "edit"} onClick={() => setMobileView("edit")}><LayoutPanelTop size={16} />Editar</button>
        <button type="button" data-active={mobileView === "preview"} onClick={() => setMobileView("preview")}><Eye size={16} />Prévia</button>
      </nav>

      <div className="osk-message-editor__workspace">
        <section className="osk-message-editor__edit-pane">
          <nav className="osk-message-editor__modes" role="tablist" aria-label="Áreas da mensagem">
            {availableModes.map((item) => {
              const ModeIcon = modeIcon(item);
              return <button key={item} type="button" role="tab" aria-selected={mode === item} data-active={mode === item} onClick={() => setMode(item)}><ModeIcon size={15} />{MODE_LABELS[item]}</button>;
            })}
          </nav>

          <div className="osk-message-editor__mode-content">
            {mode === "variables" ? (
              <MessageVariablesPanel variables={variables} insertTargetLabel={activeTextField?.label} onInsert={activeTextField ? handleInsertVariable : undefined} />
            ) : mode === "json" ? (
              <MessageJsonEditor value={jsonText} error={jsonError} dirty={jsonDirty} onChange={handleJsonChange} />
            ) : (
              <MessageVisualEditor fields={currentFields} baseline={baseline} draft={draft} guildOptions={guildOptions} onChange={onChange} onFocusField={(field) => { if (field.type === "text" || field.type === "textarea") setActiveTextFieldId(field.id); }} />
            )}
          </div>
        </section>

        <aside className="osk-message-editor__preview-pane">
          <div className="osk-message-editor__preview-label"><strong>Prévia</strong><small>Representação aproximada no Discord.</small></div>
          <MessagePreview groupLabel={groupLabel} fields={fields} draft={draft} guildOptions={guildOptions} botName={botName} botAvatarUrl={botAvatarUrl} />
        </aside>
      </div>

      <footer className="osk-message-editor__footer">
        <button type="button" className="osk-secondary-button" onClick={() => requestClose("discard")}>Cancelar</button>
        <button type="button" className="osk-primary-button" disabled={applyDisabled} onClick={handleApply}>Aplicar alterações</button>
      </footer>
    </div>
  </div>;

  return createPortal(editor, document.body);
}
