import assert from "node:assert/strict";
import test from "node:test";
import { messageEditorEscapeAction, messageEditorViewportHeight } from "../src/components/message-editor/messageEditorDialogModel.js";

test("dialog viewport uses the smallest available height and never returns zero", () => {
  assert.equal(messageEditorViewportHeight(500.4, 700, 650), 500);
  assert.equal(messageEditorViewportHeight(undefined, 700, 650), 650);
  assert.equal(messageEditorViewportHeight(0, 700, 650), 1);
  assert.equal(messageEditorViewportHeight(900, 700, 0), 700);
});

test("Escape stops inline editing before navigating auxiliary views", () => {
  assert.equal(messageEditorEscapeAction("field-1", "variables"), "stop-editing");
  assert.equal(messageEditorEscapeAction(null, "variables"), "auxiliary-back");
  assert.equal(messageEditorEscapeAction(null, "json"), "auxiliary-back");
  assert.equal(messageEditorEscapeAction(null, "canvas"), "apply");
});
