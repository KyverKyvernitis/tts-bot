import {
  Bold,
  Braces,
  ChevronLeft,
  Code2,
  Image,
  Italic,
  Link2,
  ListPlus,
  MessageSquareText,
  Pencil,
  Redo2,
  Strikethrough,
  Undo2,
  Variable,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type TransitionEvent } from "react";
import { createPortal } from "react-dom";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { MessageJsonEditor } from "./MessageJsonEditor";
import { MessagePreview } from "./MessagePreview";
import { MessageVariablesPanel } from "./MessageVariablesPanel";
import { MessageVisualEditor } from "./MessageVisualEditor";
import type {
  JsonFieldChange,
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

interface HistoryChange {
  field: DashboardFieldDefinition;
  before: unknown;
  after: unknown;
}

interface HistoryEntry {
  changes: HistoryChange[];
  mergeKey: string | null;
  at: number;
}

function valuesEqual(a: unknown, b: unknown) {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

function cloneValue<T>(value: T): T {
  if (value === undefined || value === null || typeof value !== "object") return value;
  try { return structuredClone(value); } catch {
    try { return JSON.parse(JSON.stringify(value)) as T; } catch { return value; }
  }
}

function fieldMode(fieldId: string, label: string, type: string): Exclude<MessageEditorMode, "variables" | "json"> {
  const text = `${fieldId} ${label}`.toLocaleLowerCase("pt-BR");
  if (type === "color_slots" || type === "color" || type === "url" || /(image|imagem|media|mídia|banner|avatar|author|autor|thumbnail|ícone|icon|cor)/.test(text)) return "appearance";
  if (/(button|botão|placeholder|seletor|select|component|style|estilo|emoji)/.test(text)) return "components";
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
    sectionId,
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

  const editorKey = `${sectionId}:${editorId}`;
  const jsonFields = useMemo(() => fields.filter((field) => field.type !== "color_slots"), [fields]);
  const serializedDraft = useMemo(() => serializeMessageFields(jsonFields, draft), [draft, jsonFields]);
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

  const initialField = visualFields.find((field) => field.type === "text" || field.type === "textarea") ?? visualFields[0] ?? null;
  const [visible, setVisible] = useState(false);
  const [mode, setMode] = useState<MessageEditorMode>(() => initialField ? fieldMode(initialField.id, initialField.label, initialField.type) : availableModes[0] ?? "json");
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(initialField?.id ?? null);
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);
  const [selectedColorSlot, setSelectedColorSlot] = useState<number | null>(null);
  const [jsonText, setJsonText] = useState(serializedDraft);
  const [jsonBaseline, setJsonBaseline] = useState<Record<string, unknown>>(() => messageFieldsObject(jsonFields, draft));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingJsonChanges, setPendingJsonChanges] = useState<JsonFieldChange[] | null>(null);
  const [activeTextFieldId, setActiveTextFieldId] = useState<string | null>(initialField && (initialField.type === "text" || initialField.type === "textarea") ? initialField.id : null);
  const [historyStatus, setHistoryStatus] = useState({ index: 0, length: 0 });

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const closeIntent = useRef<"apply" | "discard" | null>(null);
  const finalIntent = useRef<"apply" | "discard" | null>(null);
  const closing = useRef(false);
  const closeTimerRef = useRef<number | null>(null);
  const scrollPositionRef = useRef(0);
  const textSelectionRef = useRef<{ fieldId: string; start: number; end: number } | null>(null);
  const historyMarker = useRef(`osk-editor-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const historyRef = useRef<HistoryEntry[]>([]);
  const historyIndexRef = useRef(0);
  const latestDraftRef = useRef<Record<string, unknown>>({ ...draft });
  const closeAfterJsonApplyRef = useRef(false);

  const localDirty = useMemo(
    () => fields.some((field) => !valuesEqual(baseline[field.id], draft[field.id])),
    [baseline, draft, fields],
  );
  const localDirtyRef = useRef(localDirty);
  const jsonDirtyRef = useRef(jsonDirty);
  const pendingJsonChangesRef = useRef(pendingJsonChanges);
  const editingFieldIdRef = useRef(editingFieldId);
  const onChangeRef = useRef(onChange);
  const onApplyRef = useRef(onApply);
  const onDiscardRef = useRef(onDiscard);
  const applyActionRef = useRef<() => void>(() => undefined);

  localDirtyRef.current = localDirty;
  jsonDirtyRef.current = jsonDirty;
  pendingJsonChangesRef.current = pendingJsonChanges;
  editingFieldIdRef.current = editingFieldId;
  onChangeRef.current = onChange;
  onApplyRef.current = onApply;
  onDiscardRef.current = onDiscard;

  useEffect(() => {
    latestDraftRef.current = { ...draft };
  }, [draft]);

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

  const beginClose = useCallback((intent: "apply" | "discard") => {
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
    const intent = closeIntent.current;
    closeIntent.current = null;
    if (intent) beginClose(intent);
    else applyActionRef.current();
  }, [beginClose]);

  const requestClose = useCallback((intent: "apply" | "discard") => {
    if (closing.current) return;
    closeIntent.current = intent;
    if (window.history.state?.oskMessageEditor === historyMarker.current) window.history.back();
    else handleHistoryClose();
  }, [handleHistoryClose]);

  function updateHistoryStatus() {
    setHistoryStatus({ index: historyIndexRef.current, length: historyRef.current.length });
  }

  function recordChanges(changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>, merge = true) {
    const normalized = changes.map(({ field, raw }) => ({
      field,
      before: cloneValue(latestDraftRef.current[field.id]),
      after: cloneValue(raw),
    })).filter((change) => !valuesEqual(change.before, change.after));
    if (!normalized.length) return;

    const now = Date.now();
    const mergeKey = merge && normalized.length === 1 ? normalized[0].field.id : null;
    const entries = historyRef.current.slice(0, historyIndexRef.current);
    const last = entries[entries.length - 1];
    if (merge && mergeKey && last?.mergeKey === mergeKey && now - last.at < 750 && last.changes.length === 1) {
      last.changes[0].after = cloneValue(normalized[0].after);
      last.at = now;
    } else {
      entries.push({ changes: normalized, mergeKey, at: now });
    }
    historyRef.current = entries;
    historyIndexRef.current = entries.length;
    updateHistoryStatus();

    for (const change of normalized) {
      latestDraftRef.current[change.field.id] = cloneValue(change.after);
      onChangeRef.current(change.field, change.after);
    }
  }

  function handleFieldChange(field: DashboardFieldDefinition, raw: unknown) {
    recordChanges([{ field, raw }], true);
  }

  function undo() {
    if (historyIndexRef.current <= 0 || jsonDirtyRef.current || pendingJsonChangesRef.current) return;
    const entry = historyRef.current[historyIndexRef.current - 1];
    for (const change of [...entry.changes].reverse()) {
      latestDraftRef.current[change.field.id] = cloneValue(change.before);
      onChangeRef.current(change.field, cloneValue(change.before));
    }
    historyIndexRef.current -= 1;
    updateHistoryStatus();
    setEditingFieldId(null);
  }

  function redo() {
    if (historyIndexRef.current >= historyRef.current.length || jsonDirtyRef.current || pendingJsonChangesRef.current) return;
    const entry = historyRef.current[historyIndexRef.current];
    for (const change of entry.changes) {
      latestDraftRef.current[change.field.id] = cloneValue(change.after);
      onChangeRef.current(change.field, cloneValue(change.after));
    }
    historyIndexRef.current += 1;
    updateHistoryStatus();
    setEditingFieldId(null);
  }

  useEffect(() => {
    const first = visualFields.find((field) => field.type === "text" || field.type === "textarea") ?? visualFields[0] ?? null;
    setSelectedFieldId(first?.id ?? null);
    setEditingFieldId(null);
    setSelectedColorSlot(null);
    setMode(first ? fieldMode(first.id, first.label, first.type) : availableModes[0] ?? "json");
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    setJsonDirty(false);
    setJsonError(null);
    setPendingJsonChanges(null);
    closeAfterJsonApplyRef.current = false;
    setActiveTextFieldId(first && (first.type === "text" || first.type === "textarea") ? first.id : null);
    textSelectionRef.current = null;
    latestDraftRef.current = { ...draft };
    historyRef.current = [];
    historyIndexRef.current = 0;
    setHistoryStatus({ index: 0, length: 0 });
    closing.current = false;
    closeIntent.current = null;
    finalIntent.current = null;
    setVisible(false);
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => setVisible(true));
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [editorKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!availableModes.includes(mode)) setMode(availableModes[0] ?? "json");
  }, [availableModes, mode]);

  useEffect(() => {
    if (selectedFieldId && visualFields.some((field) => field.id === selectedFieldId)) return;
    const next = visualFields.find((field) => field.type === "text" || field.type === "textarea") ?? visualFields[0] ?? null;
    setSelectedFieldId(next?.id ?? null);
    setEditingFieldId(null);
    setActiveTextFieldId(next && (next.type === "text" || next.type === "textarea") ? next.id : null);
    textSelectionRef.current = null;
    if (next) setMode(fieldMode(next.id, next.label, next.type));
  }, [selectedFieldId, visualFields]);

  useEffect(() => {
    if (!editingFieldId) return;
    const frame = window.requestAnimationFrame(() => {
      const target = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("[data-message-inline-field-id]") ?? [])
        .find((element) => element.dataset.messageInlineFieldId === editingFieldId);
      target?.scrollIntoView({ block: "center", behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editingFieldId]);

  useEffect(() => {
    if (!jsonDirty && pendingJsonChanges === null) {
      setJsonText(serializedDraft);
      setJsonBaseline(messageFieldsObject(jsonFields, draft));
    }
  }, [draft, jsonDirty, jsonFields, pendingJsonChanges, serializedDraft]);

  useEffect(() => {
    if (!pendingJsonChanges || !pendingChangesReachedDraft(pendingJsonChanges, draft)) return;
    setPendingJsonChanges(null);
    setJsonDirty(false);
    setJsonError(null);
    setJsonText(serializeMessageFields(jsonFields, draft));
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    if (closeAfterJsonApplyRef.current) {
      closeAfterJsonApplyRef.current = false;
      requestClose("apply");
    }
  }, [draft, jsonFields, pendingJsonChanges]); // eslint-disable-line react-hooks/exhaustive-deps

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
      const viewport = window.visualViewport;
      const height = Math.max(1, Math.round(viewport?.height ?? window.innerHeight));
      dialogRef.current?.style.setProperty("--osk-message-editor-viewport-height", `${height}px`);
    };
    syncVisualViewport();
    window.visualViewport?.addEventListener("resize", syncVisualViewport);
    window.visualViewport?.addEventListener("scroll", syncVisualViewport);
    window.addEventListener("resize", syncVisualViewport);

    restoreHistoryMarker();
    window.setTimeout(() => dialogRef.current?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)")?.focus(), 0);
    const onBackRequest = () => handleHistoryClose();
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
        return;
      }
      if (event.key === "Escape") {
        if (editingFieldIdRef.current) {
          event.preventDefault();
          setEditingFieldId(null);
          return;
        }
        event.preventDefault();
        applyActionRef.current();
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
      if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
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
  }, [editorKey, handleHistoryClose, requestClose, restoreHistoryMarker]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleJsonChange(next: string) {
    setJsonText(next);
    setJsonDirty(true);
    setJsonError(null);
    setEditingFieldId(null);
    setMode("json");
  }

  function discardJson() {
    setJsonText(serializedDraft);
    setJsonBaseline(messageFieldsObject(jsonFields, draft));
    setJsonDirty(false);
    setJsonError(null);
    closeAfterJsonApplyRef.current = false;
  }

  function applyJson(closeAfter = false) {
    if (pendingJsonChanges) return;
    try {
      const changes = parseMessageJson(jsonText, jsonFields, draft, jsonBaseline);
      setJsonError(null);
      if (changes.length === 0) {
        setJsonDirty(false);
        setJsonText(serializedDraft);
        setJsonBaseline(messageFieldsObject(jsonFields, draft));
        if (closeAfter) requestClose("apply");
        return;
      }
      closeAfterJsonApplyRef.current = closeAfter;
      setPendingJsonChanges(changes);
      recordChanges(changes.map((change) => ({ field: change.field, raw: change.raw })), false);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "JSON inválido.");
      setMode("json");
      restoreHistoryMarker();
    }
  }

  function handleApply() {
    if (pendingJsonChanges) {
      restoreHistoryMarker();
      return;
    }
    if (jsonDirty) {
      applyJson(true);
      return;
    }
    requestClose("apply");
  }

  applyActionRef.current = handleApply;

  const colorPanelNumber = sectionId === "color_roles" ? Number(editorId.match(/^color-panel-([1-3])$/)?.[1] || 0) : 0;
  const colorSlotRange = colorPanelNumber ? { start: (colorPanelNumber - 1) * 10 + 1, end: colorPanelNumber * 10 } : null;
  const selectedField = fields.find((field) => field.id === selectedFieldId) ?? null;
  const activeTextField = fields.find((field) => field.id === activeTextFieldId && (field.type === "text" || field.type === "textarea"))
    ?? categorizedFields.content.find((field) => field.type === "text" || field.type === "textarea")
    ?? null;

  function handleSelectField(field: DashboardFieldDefinition) {
    if (jsonDirty || pendingJsonChanges) {
      setMode("json");
      return;
    }
    setSelectedFieldId(field.id);
    setEditingFieldId((current) => current === field.id ? current : null);
    setMode(fieldMode(field.id, field.label, field.type));
    if (field.type === "text" || field.type === "textarea") {
      setActiveTextFieldId(field.id);
      if (textSelectionRef.current?.fieldId !== field.id) textSelectionRef.current = null;
    }
  }

  function handleEditField(field: DashboardFieldDefinition) {
    if (jsonDirty || pendingJsonChanges || (field.type !== "text" && field.type !== "textarea")) return;
    handleSelectField(field);
    setEditingFieldId(field.id);
  }

  function handleTextSelection(field: DashboardFieldDefinition, start: number, end: number) {
    setActiveTextFieldId(field.id);
    textSelectionRef.current = { fieldId: field.id, start, end };
  }

  function focusTextControl(fieldId: string, start: number, end: number) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const inline = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-inline-field-id]") ?? [])
          .find((element) => element.dataset.messageInlineFieldId === fieldId);
        const form = Array.from(dialogRef.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-message-field-id]") ?? [])
          .find((element) => element.dataset.messageFieldId === fieldId);
        const control = inline ?? form;
        if (!control) return;
        control.focus({ preventScroll: true });
        control.setSelectionRange(start, end);
      });
    });
  }

  function replaceTextSelection(insertion: string, selectInserted = false) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    const start = Math.max(0, Math.min(current.length, saved?.start ?? current.length));
    const end = Math.max(start, Math.min(current.length, saved?.end ?? start));
    const next = `${current.slice(0, start)}${insertion}${current.slice(end)}`;
    const nextStart = selectInserted ? start : start + insertion.length;
    const nextEnd = start + insertion.length;
    recordChanges([{ field: activeTextField, raw: next }], true);
    textSelectionRef.current = { fieldId: activeTextField.id, start: nextStart, end: nextEnd };
    setSelectedFieldId(activeTextField.id);
    setMode(fieldMode(activeTextField.id, activeTextField.label, activeTextField.type));
    focusTextControl(activeTextField.id, nextStart, nextEnd);
  }

  function wrapText(prefix: string, suffix: string, placeholder: string) {
    if (!activeTextField) return;
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    const start = Math.max(0, Math.min(current.length, saved?.start ?? current.length));
    const end = Math.max(start, Math.min(current.length, saved?.end ?? start));
    const selected = current.slice(start, end) || placeholder;
    const insertion = `${prefix}${selected}${suffix}`;
    const next = `${current.slice(0, start)}${insertion}${current.slice(end)}`;
    const selectionStart = start + prefix.length;
    const selectionEnd = selectionStart + selected.length;
    recordChanges([{ field: activeTextField, raw: next }], true);
    textSelectionRef.current = { fieldId: activeTextField.id, start: selectionStart, end: selectionEnd };
    setSelectedFieldId(activeTextField.id);
    setMode(fieldMode(activeTextField.id, activeTextField.label, activeTextField.type));
    focusTextControl(activeTextField.id, selectionStart, selectionEnd);
  }

  function handleInsertVariable(key: string) {
    if (!variables || !activeTextField) return;
    const token = formatTemplateVariable(variables.syntax, key);
    const current = String(latestDraftRef.current[activeTextField.id] ?? "");
    const saved = textSelectionRef.current?.fieldId === activeTextField.id ? textSelectionRef.current : null;
    const separator = !saved && current && !/\s$/.test(current) ? " " : "";
    replaceTextSelection(`${separator}${token}`);
  }

  function handleModeChange(next: MessageEditorMode) {
    if (jsonDirty && next !== "json") {
      setMode("json");
      return;
    }
    setMode(next);
    setEditingFieldId(null);
    if (next === "content" || next === "appearance" || next === "components") {
      const candidates = categorizedFields[next];
      const nextField = candidates.find((field) => field.id === selectedFieldId) ?? candidates[0] ?? null;
      if (nextField) handleSelectField(nextField);
    }
  }

  const currentFields = mode === "content" || mode === "appearance" || mode === "components" ? categorizedFields[mode] : [];
  const contextField = currentFields.find((field) => field.id === selectedFieldId) ?? currentFields[0] ?? null;
  const applyDisabled = Boolean(pendingJsonChanges) || (!localDirty && !jsonDirty);
  const canvasInteractive = !jsonDirty && !pendingJsonChanges;

  function handleTransitionEnd(event: TransitionEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget || visible || !closing.current) return;
    if (event.propertyName === "opacity") finalizeClose();
  }

  const editor = <div ref={dialogRef} className="osk-root osk-message-editor osk-message-editor--unified" data-visible={visible || undefined} role="dialog" aria-modal="true" aria-label={`Editar ${groupLabel}`} onTransitionEnd={handleTransitionEnd}>
    <div className="osk-message-editor__shell">
      <header className="osk-message-editor__header">
        <button type="button" className="osk-message-editor__back" onClick={handleApply} aria-label="Aplicar e voltar"><ChevronLeft size={19} /></button>
        <div className="osk-message-editor__title">
          <small>{sectionLabel}</small>
          <strong>{groupLabel}</strong>
          {description && <p>{description}</p>}
        </div>
        <div className="osk-message-editor__header-actions">
          <button type="button" disabled={historyStatus.index <= 0 || jsonDirty || Boolean(pendingJsonChanges)} onClick={undo} aria-label="Desfazer"><Undo2 size={17} /></button>
          <button type="button" disabled={historyStatus.index >= historyStatus.length || jsonDirty || Boolean(pendingJsonChanges)} onClick={redo} aria-label="Refazer"><Redo2 size={17} /></button>
          <span className="osk-message-editor__dirty" data-visible={localDirty || jsonDirty || undefined} aria-live="polite" aria-hidden={!(localDirty || jsonDirty)}>Alterado</span>
        </div>
      </header>

      <div className="osk-message-editor__workspace">
        <section className="osk-message-editor__canvas-pane">
          <div className="osk-message-editor__canvas-head">
            <div><strong>Mensagem</strong><small>Selecione um elemento. Toque novamente em um texto para editar diretamente.</small></div>
            {selectedField && <span>{selectedField.label}</span>}
          </div>
          <MessagePreview
            sectionId={sectionId}
            editorId={editorId}
            groupLabel={groupLabel}
            fields={fields}
            draft={draft}
            guildOptions={guildOptions}
            botName={botName}
            botAvatarUrl={botAvatarUrl}
            interactive={canvasInteractive}
            selectedFieldId={selectedFieldId}
            editingFieldId={editingFieldId}
            selectedColorSlot={selectedColorSlot}
            textSelection={textSelectionRef.current}
            onSelectField={handleSelectField}
            onEditField={handleEditField}
            onFinishEdit={() => setEditingFieldId(null)}
            onChange={handleFieldChange}
            onTextSelection={handleTextSelection}
            onSelectColorSlot={(slotNumber) => setSelectedColorSlot(slotNumber)}
          />
          {!canvasInteractive && <div className="osk-message-editor__canvas-lock"><Braces size={17} /><span>Aplique ou descarte o JSON pendente para voltar à edição visual.</span></div>}
        </section>

        <aside className="osk-message-editor__context-pane">
          <nav className="osk-message-editor__modes" role="tablist" aria-label="Áreas da mensagem">
            {availableModes.map((item) => {
              const ModeIcon = modeIcon(item);
              return <button key={item} type="button" role="tab" aria-selected={mode === item} data-active={mode === item || undefined} disabled={jsonDirty && item !== "json"} onClick={() => handleModeChange(item)}><ModeIcon size={15} />{MODE_LABELS[item]}</button>;
            })}
          </nav>

          <div className="osk-message-editor__mode-content">
            {mode === "variables" ? (
              <MessageVariablesPanel variables={variables} insertTargetLabel={activeTextField?.label} onInsert={activeTextField ? handleInsertVariable : undefined} />
            ) : mode === "json" ? (
              <MessageJsonEditor value={jsonText} error={jsonError} dirty={jsonDirty} applying={Boolean(pendingJsonChanges)} onChange={handleJsonChange} onApply={() => applyJson(false)} onDiscard={discardJson} />
            ) : contextField ? (
              <>
                <div className="osk-message-editor__field-tabs" aria-label={`Campos de ${MODE_LABELS[mode]}`}>
                  {currentFields.map((field) => <button type="button" key={field.id} data-active={contextField.id === field.id || undefined} onClick={() => handleSelectField(field)}>{field.label}</button>)}
                </div>
                {(contextField.type === "text" || contextField.type === "textarea") && (
                  <div className="osk-message-editor__text-tools" aria-label="Formatação de texto">
                    <button type="button" onClick={() => handleEditField(contextField)} title="Editar na mensagem"><Pencil size={15} /></button>
                    <span />
                    <button type="button" onClick={() => wrapText("**", "**", "texto")} title="Negrito"><Bold size={15} /></button>
                    <button type="button" onClick={() => wrapText("*", "*", "texto")} title="Itálico"><Italic size={15} /></button>
                    <button type="button" onClick={() => wrapText("~~", "~~", "texto")} title="Tachado"><Strikethrough size={15} /></button>
                    <button type="button" onClick={() => wrapText("`", "`", "código")} title="Código"><Code2 size={15} /></button>
                    <button type="button" onClick={() => wrapText("[", "](https://)", "texto do link")} title="Link"><Link2 size={15} /></button>
                    {variables?.items.length ? <button type="button" onClick={() => setMode("variables")} title="Inserir variável"><Variable size={15} /></button> : null}
                  </div>
                )}
                <MessageVisualEditor
                  fields={[contextField]}
                  baseline={baseline}
                  draft={draft}
                  guildOptions={guildOptions}
                  onChange={handleFieldChange}
                  selectedFieldId={selectedFieldId}
                  selectedColorSlot={selectedColorSlot}
                  colorSlotRange={colorSlotRange}
                  onColorSlotSelect={(slotNumber) => setSelectedColorSlot(slotNumber)}
                  onFocusField={(field) => {
                    setSelectedFieldId(field.id);
                    if (field.type !== "text" && field.type !== "textarea") return;
                    setActiveTextFieldId(field.id);
                    if (textSelectionRef.current?.fieldId !== field.id) textSelectionRef.current = null;
                  }}
                  onTextSelection={handleTextSelection}
                />
              </>
            ) : (
              <div className="osk-message-empty">Nenhum campo está disponível nesta área.</div>
            )}
          </div>
        </aside>
      </div>

      <footer className="osk-message-editor__footer">
        <button type="button" className="osk-secondary-button" onClick={() => requestClose("discard")}>Descartar</button>
        <button type="button" className="osk-primary-button" disabled={applyDisabled} onClick={handleApply}>{pendingJsonChanges ? "Aplicando..." : "Aplicar ao painel"}</button>
      </footer>
    </div>
  </div>;

  return createPortal(editor, document.body);
}
