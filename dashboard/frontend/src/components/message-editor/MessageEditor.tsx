import { useMemo, useRef } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { createPortal } from "react-dom";
import { useMessageEditorContextPlacement } from "./useMessageEditorContextPlacement";
import { useMessageEditorDialogLifecycle } from "./useMessageEditorDialogLifecycle";
import { useMessageEditorHistory } from "./useMessageEditorHistory";
import { useMessageEditorJsonState } from "./useMessageEditorJsonState";
import { useMessageEditorJsonWorkflow } from "./useMessageEditorJsonWorkflow";
import { useMessageEditorNavigation } from "./useMessageEditorNavigation";
import { useMessageEditorResetEffect } from "./useMessageEditorResetEffect";
import { useMessageEditorTextEditing } from "./useMessageEditorTextEditing";
import { useMessageEditorWorkspaceEffects } from "./useMessageEditorWorkspaceEffects";
import { useMessageEditorViewState } from "./useMessageEditorViewState";
import { useMessageEditorInitialField } from "./useMessageEditorInitialField";
import { MessageEditorSurface } from "./MessageEditorSurface";
import { messageEditorDerivedState } from "./messageEditorDerivedState";
import { messageEditorFieldGroups, messageEditorHasFieldChanges } from "./messageEditorFields";
import type { MessageEditorProps } from "./messageEditorTypes";
import {
  relatedMessageEditorContextFields,
} from "./messageEditorModel";

const EMPTY_SENDER_FIELDS: string[] = [];
export function MessageEditor(props: MessageEditorProps) {
  const {
    editorId,
    sectionId,
    sectionLabel,
    groupLabel,
    description,
    fields,
    senderFieldIds = EMPTY_SENDER_FIELDS,
    presentation = "generic",
    baseline,
    draft,
    guildOptions,
    botName,
    botAvatarUrl,
    guildName,
    guildAvatarUrl,
    variables,
    onChange,
    onApply,
    onDiscard,
  } = props;

  const editorKey = `${sectionId}:${editorId}`;
  const senderFieldIdSet = useMemo(() => new Set(senderFieldIds), [senderFieldIds]);
  const { jsonFields, visualFields, senderFields, messageFields } = useMemo(
    () => messageEditorFieldGroups(fields, senderFieldIds, editorId, draft, props.focusFieldId),
    [draft, editorId, fields, senderFieldIds, props.focusFieldId],
  );
  const {
    view,
    setView,
    selectedFieldId,
    setSelectedFieldId,
    editingFieldId,
    setEditingFieldId,
    selectedColorSlot,
    setSelectedColorSlot,
    contextAnchorFieldId,
    setContextAnchorFieldId,
    resetViewState,
  } = useMessageEditorViewState();

  const dialogRef = useRef<HTMLDivElement>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const canvasPaneRef = useRef<HTMLElement>(null);
  const contextPanelRef = useRef<HTMLElement>(null);

  const {
    serializedDraft,
    jsonText,
    jsonBaseline,
    jsonDirty,
    jsonError,
    pendingJsonChanges,
    closeAfterJsonApplyRef,
    setJsonDirty,
    setJsonError,
    setPendingJsonChanges,
    syncFromDraft: syncJsonFromDraft,
    resetJson,
    markJsonChanged,
    discardJson,
  } = useMessageEditorJsonState({ jsonFields, draft });

  const localDirty = useMemo(
    () => messageEditorHasFieldChanges(fields, baseline, draft),
    [baseline, draft, fields],
  );


  const {
    status: historyStatus,
    latestDraftRef,
    recordChanges,
    undo,
    redo,
    reset: resetHistory,
  } = useMessageEditorHistory({
    draft,
    blocked: Boolean(jsonDirty || pendingJsonChanges),
    onChange,
    onEditingStop: () => setEditingFieldId(null),
  });

  const {
    activeTextField,
    activeEditingField,
    activeEditingValue,
    textToolNotice,
    textSelectionRef,
    setActiveTextFieldId,
    clearTextSelection,
    resetTextEditing,
    handleTextSelection,
    wrapText,
    prefixTextLines,
    insertVariable: handleInsertVariable,
  } = useMessageEditorTextEditing({
    fields,
    draft,
    variables,
    editingFieldId,
    dialogRef,
    latestDraftRef,
    recordChanges,
    setSelectedFieldId,
    setEditingFieldId,
    setView,
  });

  function handleFieldChange(field: DashboardFieldDefinition, raw: unknown) {
    recordChanges([{ field, raw }], true);
  }

  const {
    visible,
    requestClose,
    restoreHistoryMarker,
    applyActionRef,
    auxiliaryBackRef,
    handleTransitionEnd,
  } = useMessageEditorDialogLifecycle({
    editorKey,
    dialogRef,
    view,
    editingFieldId,
    localDirty,
    jsonDirty,
    onApply,
    onDiscard,
    onUndo: undo,
    onRedo: redo,
    onStopEditing: () => setEditingFieldId(null),
  });

  const { contextPlacement, setContextPlacement } = useMessageEditorContextPlacement({
    view,
    contextAnchorFieldId,
    draft,
    workspaceRef,
    canvasPaneRef,
    contextPanelRef,
  });

  useMessageEditorResetEffect({
    editorKey,
    draft,
    resetViewState,
    setContextPlacement,
    resetJson,
    resetTextEditing,
    resetHistory,
  });

  useMessageEditorWorkspaceEffects({
    view,
    selectedFieldId,
    visualFields,
    contextPanelRef,
    canvasPaneRef,
    setSelectedFieldId,
    setContextAnchorFieldId,
    setContextPlacement,
    setEditingFieldId,
    setView,
    setActiveTextFieldId,
    clearTextSelection,
  });

  useMessageEditorInitialField({ editorKey, fieldId: props.focusFieldId, fields, setSelectedFieldId, setContextAnchorFieldId, setView });
  const { handleJsonChange, applyJson, handleApply } = useMessageEditorJsonWorkflow({
    jsonFields,
    draft,
    serializedDraft,
    jsonText,
    jsonBaseline,
    jsonDirty,
    pendingJsonChanges,
    closeAfterJsonApplyRef,
    setJsonDirty,
    setJsonError,
    setPendingJsonChanges,
    syncJsonFromDraft,
    markJsonChanged,
    recordChanges,
    requestClose,
    restoreHistoryMarker,
    setEditingFieldId,
    setView,
  });

  applyActionRef.current = handleApply;

  const {
    selectedField,
    senderSelected,
    inspectorFields,
    openSenderInspector,
    handleSelectField,
    handleEditField,
    openVariables,
    openJson,
    openInspector,
    leaveAuxiliaryView,
  } = useMessageEditorNavigation({
    fields,
    visualFields,
    senderFields,
    senderFieldIdSet,
    presentation,
    jsonDirty,
    pendingJsonChanges,
    view,
    selectedFieldId,
    editingFieldId,
    textSelectionRef,
    setView,
    setSelectedFieldId,
    setEditingFieldId,
    setContextAnchorFieldId,
    setContextPlacement,
    setActiveTextFieldId,
    clearTextSelection,
    discardJson,
  });
  const {
    activeColorPanel,
    colorSlotIds,
    senderEnabled,
    applyDisabled,
    canvasInteractive,
    contextTitle,
    contextDescription,
  } = messageEditorDerivedState({
    sectionId,
    editorId,
    draft,
    jsonDirty,
    pendingJsonChanges: Boolean(pendingJsonChanges),
    view,
    senderSelected,
    presentation,
    selectedColorSlot,
    inspectorFields,
    selectedField,
    activeTextField,
  });

  auxiliaryBackRef.current = leaveAuxiliaryView;

  const editor = <MessageEditorSurface
    dialogRef={dialogRef}
    workspaceRef={workspaceRef}
    visible={visible}
    view={view}
    textEditing={Boolean(activeEditingField)}
    groupLabel={groupLabel}
    onTransitionEnd={handleTransitionEnd}
    header={{
      view,
      sectionLabel,
      groupLabel,
      historyIndex: historyStatus.index,
      historyLength: historyStatus.length,
      jsonDirty,
      pendingJson: Boolean(pendingJsonChanges),
      localDirty,
      hasVariables: Boolean(variables?.items.length),
      presentation,
      onBack: view === "canvas" ? handleApply : leaveAuxiliaryView,
      onUndo: undo,
      onRedo: redo,
      onVariables: openVariables,
      onJson: openJson,
    }}
    canvas={{
      canvasPaneRef,
      interactive: canvasInteractive,
      hidden: view !== "canvas",
      preview: {
        sectionId,
        editorId,
        groupLabel,
        presentation,
        fields: messageFields,
        senderFields,
        draft,
        guildOptions,
        botName,
        botAvatarUrl,
        guildName,
        guildAvatarUrl,
        interactive: canvasInteractive,
        senderSelected,
        onSelectSender: openSenderInspector,
        onEditSender: openSenderInspector,
        selectedFieldId,
        editingFieldId,
        selectedColorSlot,
        textSelection: textSelectionRef.current,
        onSelectField: handleSelectField,
        onEditField: handleEditField,
        hasFieldOptions: (field) => relatedMessageEditorContextFields(field, visualFields).length > 1,
        onOpenFieldOptions: openInspector,
        onFinishEdit: () => setEditingFieldId(null),
        onChange: handleFieldChange,
        onTextSelection: handleTextSelection,
        onSelectColorSlot: (slotNumber) => {
          setSelectedColorSlot(slotNumber);
          setSelectedFieldId("color_roles.slots");
          setContextAnchorFieldId("color_roles.slots");
          setEditingFieldId(null);
          setActiveTextFieldId(null);
          setView("inspector");
        },
      },
    }}
    context={view === "canvas" ? null : {
      view,
      anchorFieldId: contextAnchorFieldId,
      panelRef: contextPanelRef,
      placement: contextPlacement,
      title: contextTitle,
      description: contextDescription,
      pendingJson: Boolean(pendingJsonChanges),
      onClose: leaveAuxiliaryView,
      variables,
      activeTextFieldLabel: activeTextField?.label,
      onInsertVariable: activeTextField ? handleInsertVariable : undefined,
      jsonText,
      jsonError,
      jsonDirty,
      onJsonChange: handleJsonChange,
      onJsonApply: () => applyJson(false),
      onJsonDiscard: () => { discardJson(); setView("canvas"); },
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
      onColorSlotSelect: setSelectedColorSlot,
      onFocusField: (field) => {
        setSelectedFieldId(field.id);
        if (field.type !== "text" && field.type !== "textarea") return;
        setActiveTextFieldId(field.id);
        if (textSelectionRef.current?.fieldId !== field.id) clearTextSelection();
      },
      onTextSelection: handleTextSelection,
      onFieldChange: handleFieldChange,
    }}
    textDock={activeEditingField ? {
      field: activeEditingField,
      value: activeEditingValue,
      notice: textToolNotice,
      hasVariables: Boolean(variables?.items.length),
      onWrap: wrapText,
      onPrefixLines: prefixTextLines,
      onVariables: openVariables,
      onDone: () => setEditingFieldId(null),
    } : null}
    pendingJson={Boolean(pendingJsonChanges)}
    dirty={localDirty || jsonDirty}
    onDiscard={() => requestClose("discard")}
    onApply={handleApply}
  />;

  return createPortal(editor, document.body);
}
