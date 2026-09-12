import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardFieldDefinition } from "../src/types/dashboard";
import {
  contextualMessageOptions,
  isMessagePreviewImageUrlField,
  messageVisualValuesEqual,
} from "../src/components/message-editor/messageVisualEditorModel";

function field(id: string, type: DashboardFieldDefinition["type"] = "text", options?: DashboardFieldDefinition["options"]): DashboardFieldDefinition {
  return { id, path: id, label: id, type, scope: "guild", options };
}

test("compara valores estruturados usados para indicador de alteração", () => {
  assert.equal(messageVisualValuesEqual({ a: 1 }, { a: 1 }), true);
  assert.equal(messageVisualValuesEqual([1, 2], [1, 3]), false);
});

test("reconhece apenas campos URL usados como preview de imagem", () => {
  assert.equal(isMessagePreviewImageUrlField(field("welcome.embed.image_url", "url")), true);
  assert.equal(isMessagePreviewImageUrlField(field("welcome.webhook.avatar_url", "url")), true);
  assert.equal(isMessagePreviewImageUrlField(field("support_url", "url")), false);
  assert.equal(isMessagePreviewImageUrlField(field("welcome.embed.image_url", "text")), false);
});

test("preserva valor atual desconhecido no select contextual sem duplicar opções", () => {
  const select = field("mode", "select", [{ value: "a", label: "A" }, { value: "b", label: "B" }]);
  assert.deepEqual(contextualMessageOptions(select, "a"), select.options);
  assert.deepEqual(contextualMessageOptions(select, "legacy").map((option) => option.value), ["legacy", "a", "b"]);
  assert.deepEqual(contextualMessageOptions(select, "").map((option) => option.value), ["a", "b"]);
});
