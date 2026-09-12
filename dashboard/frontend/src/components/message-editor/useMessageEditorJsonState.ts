import { useMemo, useRef, useState } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import type { JsonFieldChange } from "./messageEditorTypes";
import {
  messageFieldsObject,
  serializeMessageFields,
} from "./messageEditorUtils";

interface UseMessageEditorJsonStateOptions {
  jsonFields: DashboardFieldDefinition[];
  draft: Record<string, unknown>;
}

export function useMessageEditorJsonState({ jsonFields, draft }: UseMessageEditorJsonStateOptions) {
  const serializedDraft = useMemo(() => serializeMessageFields(jsonFields, draft), [draft, jsonFields]);
  const [jsonText, setJsonText] = useState(serializedDraft);
  const [jsonBaseline, setJsonBaseline] = useState<Record<string, unknown>>(() => messageFieldsObject(jsonFields, draft));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pendingJsonChanges, setPendingJsonChanges] = useState<JsonFieldChange[] | null>(null);
  const closeAfterJsonApplyRef = useRef(false);

  function syncFromDraft(nextDraft = draft) {
    setJsonText(serializeMessageFields(jsonFields, nextDraft));
    setJsonBaseline(messageFieldsObject(jsonFields, nextDraft));
  }

  function resetJson(nextDraft = draft) {
    syncFromDraft(nextDraft);
    setJsonDirty(false);
    setJsonError(null);
    setPendingJsonChanges(null);
    closeAfterJsonApplyRef.current = false;
  }

  function markJsonChanged(next: string) {
    setJsonText(next);
    setJsonDirty(true);
    setJsonError(null);
  }

  function discardJson() {
    syncFromDraft();
    setJsonDirty(false);
    setJsonError(null);
    closeAfterJsonApplyRef.current = false;
  }

  return {
    serializedDraft,
    jsonText,
    jsonBaseline,
    jsonDirty,
    jsonError,
    pendingJsonChanges,
    closeAfterJsonApplyRef,
    setJsonText,
    setJsonBaseline,
    setJsonDirty,
    setJsonError,
    setPendingJsonChanges,
    syncFromDraft,
    resetJson,
    markJsonChanged,
    discardJson,
  };
}
