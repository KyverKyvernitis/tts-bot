import type { DashboardFieldDefinition } from "../../types/dashboard";
import {
  cloneMessageEditorValue,
  messageEditorValuesEqual,
  type MessageEditorHistoryChange,
  type MessageEditorHistoryEntry,
} from "./messageEditorModel";

export interface MessageEditorHistoryState {
  entries: MessageEditorHistoryEntry[];
  index: number;
}

export function createMessageEditorHistoryChanges(
  draft: Record<string, unknown>,
  changes: Array<{ field: DashboardFieldDefinition; raw: unknown }>,
): MessageEditorHistoryChange[] {
  return changes.map(({ field, raw }) => ({
    field,
    before: cloneMessageEditorValue(draft[field.id]),
    after: cloneMessageEditorValue(raw),
  })).filter((change) => !messageEditorValuesEqual(change.before, change.after));
}

export function appendMessageEditorHistory(
  state: MessageEditorHistoryState,
  changes: MessageEditorHistoryChange[],
  options: { merge?: boolean; at?: number; mergeWindowMs?: number } = {},
): MessageEditorHistoryState {
  if (!changes.length) return state;
  const merge = options.merge ?? true;
  const at = options.at ?? Date.now();
  const mergeWindowMs = options.mergeWindowMs ?? 750;
  const mergeKey = merge && changes.length === 1 ? changes[0].field.id : null;
  const entries = state.entries.slice(0, state.index);
  const last = entries[entries.length - 1];

  if (merge && mergeKey && last?.mergeKey === mergeKey && at - last.at < mergeWindowMs && last.changes.length === 1) {
    entries[entries.length - 1] = {
      ...last,
      at,
      changes: [{ ...last.changes[0], after: cloneMessageEditorValue(changes[0].after) }],
    };
  } else {
    entries.push({
      changes: changes.map((change) => ({
        ...change,
        before: cloneMessageEditorValue(change.before),
        after: cloneMessageEditorValue(change.after),
      })),
      mergeKey,
      at,
    });
  }

  return { entries, index: entries.length };
}

export function messageEditorUndoEntry(state: MessageEditorHistoryState): {
  entry: MessageEditorHistoryEntry;
  state: MessageEditorHistoryState;
} | null {
  if (state.index <= 0) return null;
  return {
    entry: state.entries[state.index - 1],
    state: { entries: state.entries, index: state.index - 1 },
  };
}

export function messageEditorRedoEntry(state: MessageEditorHistoryState): {
  entry: MessageEditorHistoryEntry;
  state: MessageEditorHistoryState;
} | null {
  if (state.index >= state.entries.length) return null;
  return {
    entry: state.entries[state.index],
    state: { entries: state.entries, index: state.index + 1 },
  };
}
