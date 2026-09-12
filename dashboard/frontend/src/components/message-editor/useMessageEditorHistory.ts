import { useEffect, useRef, useState } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";
import { cloneMessageEditorValue } from "./messageEditorModel";
import {
  appendMessageEditorHistory,
  createMessageEditorHistoryChanges,
  messageEditorRedoEntry,
  messageEditorUndoEntry,
  type MessageEditorHistoryState,
} from "./messageEditorHistory";

interface UseMessageEditorHistoryOptions {
  draft: Record<string, unknown>;
  blocked: boolean;
  onChange(field: DashboardFieldDefinition, raw: unknown): void;
  onEditingStop(): void;
}

export function useMessageEditorHistory({ draft, blocked, onChange, onEditingStop }: UseMessageEditorHistoryOptions) {
  const [status, setStatus] = useState({ index: 0, length: 0 });
  const historyRef = useRef<MessageEditorHistoryState>({ entries: [], index: 0 });
  const latestDraftRef = useRef<Record<string, unknown>>({ ...draft });
  const blockedRef = useRef(blocked);
  const onChangeRef = useRef(onChange);
  const onEditingStopRef = useRef(onEditingStop);

  blockedRef.current = blocked;
  onChangeRef.current = onChange;
  onEditingStopRef.current = onEditingStop;

  useEffect(() => {
    latestDraftRef.current = { ...draft };
  }, [draft]);

  function updateStatus() {
    setStatus({ index: historyRef.current.index, length: historyRef.current.entries.length });
  }

  function recordChanges(changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>, merge = true) {
    const normalized = createMessageEditorHistoryChanges(latestDraftRef.current, changes);
    if (!normalized.length) return;
    historyRef.current = appendMessageEditorHistory(historyRef.current, normalized, { merge });
    updateStatus();

    for (const change of normalized) {
      latestDraftRef.current[change.field.id] = cloneMessageEditorValue(change.after);
      onChangeRef.current(change.field, change.after);
    }
  }

  function undo() {
    if (blockedRef.current) return;
    const result = messageEditorUndoEntry(historyRef.current);
    if (!result) return;
    for (const change of [...result.entry.changes].reverse()) {
      latestDraftRef.current[change.field.id] = cloneMessageEditorValue(change.before);
      onChangeRef.current(change.field, cloneMessageEditorValue(change.before));
    }
    historyRef.current = result.state;
    updateStatus();
    onEditingStopRef.current();
  }

  function redo() {
    if (blockedRef.current) return;
    const result = messageEditorRedoEntry(historyRef.current);
    if (!result) return;
    for (const change of result.entry.changes) {
      latestDraftRef.current[change.field.id] = cloneMessageEditorValue(change.after);
      onChangeRef.current(change.field, cloneMessageEditorValue(change.after));
    }
    historyRef.current = result.state;
    updateStatus();
    onEditingStopRef.current();
  }

  function reset(nextDraft: Record<string, unknown>) {
    latestDraftRef.current = { ...nextDraft };
    historyRef.current = { entries: [], index: 0 };
    setStatus({ index: 0, length: 0 });
  }

  return { status, latestDraftRef, recordChanges, undo, redo, reset };
}
