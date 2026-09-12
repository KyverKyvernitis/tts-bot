import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardFieldDefinition } from "../src/types/dashboard";
import {
  appendMessageEditorHistory,
  createMessageEditorHistoryChanges,
  messageEditorRedoEntry,
  messageEditorUndoEntry,
  type MessageEditorHistoryState,
} from "../src/components/message-editor/messageEditorHistory";

const field = (id: string): DashboardFieldDefinition => ({ id, label: id, type: "text", scope: "guild", path: id });

test("ignora alterações semanticamente idênticas e clona objetos", () => {
  const draft = { a: { nested: 1 } };
  assert.deepEqual(createMessageEditorHistoryChanges(draft, [{ field: field("a"), raw: { nested: 1 } }]), []);

  const raw = { nested: 2 };
  const changes = createMessageEditorHistoryChanges(draft, [{ field: field("a"), raw }]);
  assert.equal(changes.length, 1);
  assert.notEqual(changes[0].before, draft.a);
  assert.notEqual(changes[0].after, raw);
  raw.nested = 3;
  assert.deepEqual(changes[0].after, { nested: 2 });
});

test("mescla digitação consecutiva no mesmo campo dentro da janela", () => {
  const first = createMessageEditorHistoryChanges({ a: "" }, [{ field: field("a"), raw: "x" }]);
  let state = appendMessageEditorHistory({ entries: [], index: 0 }, first, { at: 1000 });
  const second = createMessageEditorHistoryChanges({ a: "x" }, [{ field: field("a"), raw: "xy" }]);
  state = appendMessageEditorHistory(state, second, { at: 1500 });

  assert.equal(state.entries.length, 1);
  assert.equal(state.index, 1);
  assert.equal(state.entries[0].changes[0].before, "");
  assert.equal(state.entries[0].changes[0].after, "xy");
});

test("não mescla após janela, entre campos ou quando merge está desativado", () => {
  let state: MessageEditorHistoryState = { entries: [], index: 0 };
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ a: "" }, [{ field: field("a"), raw: "x" }]), { at: 0 });
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ a: "x" }, [{ field: field("a"), raw: "xy" }]), { at: 800 });
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ b: "" }, [{ field: field("b"), raw: "z" }]), { at: 900 });
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ b: "z" }, [{ field: field("b"), raw: "zz" }]), { at: 950, merge: false });
  assert.equal(state.entries.length, 4);
});

test("nova edição após undo descarta ramo de redo", () => {
  let state: MessageEditorHistoryState = { entries: [], index: 0 };
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ a: "" }, [{ field: field("a"), raw: "1" }]), { at: 0, merge: false });
  state = appendMessageEditorHistory(state, createMessageEditorHistoryChanges({ a: "1" }, [{ field: field("a"), raw: "2" }]), { at: 1, merge: false });
  const undone = messageEditorUndoEntry(state);
  assert.ok(undone);
  state = appendMessageEditorHistory(undone.state, createMessageEditorHistoryChanges({ a: "1" }, [{ field: field("a"), raw: "3" }]), { at: 2, merge: false });
  assert.equal(state.entries.length, 2);
  assert.equal(state.entries[1].changes[0].after, "3");
  assert.equal(messageEditorRedoEntry(state), null);
});

test("undo e redo avançam índice sem alterar entradas", () => {
  const state = appendMessageEditorHistory(
    { entries: [], index: 0 },
    createMessageEditorHistoryChanges({ a: "before" }, [{ field: field("a"), raw: "after" }]),
    { at: 10, merge: false },
  );
  const undo = messageEditorUndoEntry(state);
  assert.ok(undo);
  assert.equal(undo.state.index, 0);
  assert.equal(undo.entry.changes[0].before, "before");
  const redo = messageEditorRedoEntry(undo.state);
  assert.ok(redo);
  assert.equal(redo.state.index, 1);
  assert.equal(redo.entry.changes[0].after, "after");
});
