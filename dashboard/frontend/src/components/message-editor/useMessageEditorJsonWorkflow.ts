import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import type { JsonFieldChange } from "./messageEditorTypes";
import type { MessageEditorView } from "./messageEditorModel";
import { parseMessageJson, pendingChangesReachedDraft } from "./messageEditorUtils";

interface UseMessageEditorJsonWorkflowOptions {
  jsonFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
  serializedDraft: string;
  jsonText: string;
  jsonBaseline: Record<string, unknown>;
  jsonDirty: boolean;
  pendingJsonChanges: JsonFieldChange[] | null;
  closeAfterJsonApplyRef: MutableRefObject<boolean>;
  setJsonDirty: Dispatch<SetStateAction<boolean>>;
  setJsonError: Dispatch<SetStateAction<string | null>>;
  setPendingJsonChanges: Dispatch<SetStateAction<JsonFieldChange[] | null>>;
  syncJsonFromDraft(nextDraft?: Record<string, unknown>): void;
  markJsonChanged(next: string): void;
  recordChanges(changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>, merge?: boolean): void;
  requestClose(action: "apply" | "discard"): void;
  restoreHistoryMarker(): void;
  setEditingFieldId: Dispatch<SetStateAction<string | null>>;
  setView: Dispatch<SetStateAction<MessageEditorView>>;
}

export function useMessageEditorJsonWorkflow(options: UseMessageEditorJsonWorkflowOptions) {
  const {
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
  } = options;

  useEffect(() => {
    if (!jsonDirty && pendingJsonChanges === null) syncJsonFromDraft(draft);
  }, [draft, jsonDirty, jsonFields, pendingJsonChanges, serializedDraft]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!pendingJsonChanges || !pendingChangesReachedDraft(pendingJsonChanges, draft)) return;
    setPendingJsonChanges(null);
    setJsonDirty(false);
    setJsonError(null);
    syncJsonFromDraft(draft);
    if (closeAfterJsonApplyRef.current) {
      closeAfterJsonApplyRef.current = false;
      requestClose("apply");
    } else {
      setView("canvas");
    }
  }, [draft, jsonFields, pendingJsonChanges]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleJsonChange(next: string) {
    markJsonChanged(next);
    setEditingFieldId(null);
    setView("json");
  }

  function applyJson(closeAfter = false) {
    if (pendingJsonChanges) return;
    try {
      const changes = parseMessageJson(jsonText, jsonFields, draft, jsonBaseline);
      setJsonError(null);
      if (changes.length === 0) {
        setJsonDirty(false);
        syncJsonFromDraft(draft);
        if (closeAfter) requestClose("apply");
        else setView("canvas");
        return;
      }
      closeAfterJsonApplyRef.current = closeAfter;
      setPendingJsonChanges(changes);
      recordChanges(changes.map((change) => ({ field: change.field, raw: change.raw })), false);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "JSON inválido.");
      setView("json");
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

  return { handleJsonChange, applyJson, handleApply };
}
