import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardFieldDefinition } from "../src/types/dashboard";
import {
  messageEditorCanInlineEdit,
  messageEditorContextCopy,
  messageEditorSelectFieldAction,
} from "../src/components/message-editor/messageEditorInteractionModel";

const field = (id: string, type: DashboardFieldDefinition["type"] = "text"): DashboardFieldDefinition => ({
  id, label: id, type, scope: "guild", path: id,
});

test("bloqueio por JSON envia seleção para view JSON sem reescrever estado", () => {
  assert.deepEqual(messageEditorSelectFieldAction({
    field: field("x"), blocked: true, presentation: "generic", currentEditingFieldId: "x", currentTextSelectionFieldId: "x",
  }), { kind: "blocked", view: "json" });
});

test("campo textual permanece no canvas e preserva seleção do mesmo campo", () => {
  assert.deepEqual(messageEditorSelectFieldAction({
    field: field("body", "textarea"), blocked: false, presentation: "generic", currentEditingFieldId: "body", currentTextSelectionFieldId: "body",
  }), {
    kind: "selected", selectedFieldId: "body", editingFieldId: "body", contextAnchorFieldId: null,
    activeTextFieldId: "body", view: "canvas", clearTextSelection: false,
  });
});

test("campo não textual abre inspector e limpa seleção de texto", () => {
  assert.deepEqual(messageEditorSelectFieldAction({
    field: field("style", "select"), blocked: false, presentation: "generic", currentEditingFieldId: "body", currentTextSelectionFieldId: "body",
  }), {
    kind: "selected", selectedFieldId: "style", editingFieldId: null, contextAnchorFieldId: "style",
    activeTextFieldId: null, view: "inspector", clearTextSelection: true,
  });
});

test("slots do painel de cores ficam no canvas", () => {
  const action = messageEditorSelectFieldAction({
    field: field("color_roles.slots", "color_slots"), blocked: false, presentation: "color_panel", currentEditingFieldId: null, currentTextSelectionFieldId: null,
  });
  assert.equal(action.kind, "selected");
  if (action.kind === "selected") {
    assert.equal(action.view, "canvas");
    assert.equal(action.contextAnchorFieldId, null);
  }
});

test("edição inline aceita apenas text/textarea quando não bloqueado", () => {
  assert.equal(messageEditorCanInlineEdit(field("a", "text"), false), true);
  assert.equal(messageEditorCanInlineEdit(field("a", "textarea"), false), true);
  assert.equal(messageEditorCanInlineEdit(field("a", "select"), false), false);
  assert.equal(messageEditorCanInlineEdit(field("a", "text"), true), false);
});

test("copy contextual preserva títulos de variáveis, JSON, remetente e cores", () => {
  assert.deepEqual(messageEditorContextCopy({ view: "variables", senderSelected: false, presentation: "generic", selectedColorSlot: null, activeTextFieldLabel: "Título" }), {
    title: "Variáveis", description: "Inserir em Título",
  });
  assert.deepEqual(messageEditorContextCopy({ view: "json", senderSelected: false, presentation: "generic", selectedColorSlot: null }), {
    title: "JSON avançado", description: "Edição técnica da mensagem",
  });
  assert.deepEqual(messageEditorContextCopy({ view: "inspector", senderSelected: true, presentation: "generic", selectedColorSlot: null }), {
    title: "Remetente da mensagem", description: "Nome e avatar usados no envio",
  });
  assert.deepEqual(messageEditorContextCopy({ view: "inspector", senderSelected: false, presentation: "color_panel", selectedColorSlot: 8, activeColorPanelSlots: [3, 8, 10] }), {
    title: "Opção 2", description: "Alterações aparecem imediatamente na prévia",
  });
});

test("copy contextual usa label do inspector e fallback estável", () => {
  assert.equal(messageEditorContextCopy({ view: "inspector", senderSelected: false, presentation: "generic", selectedColorSlot: null, inspectorLabel: "Cor" }).title, "Cor");
  assert.equal(messageEditorContextCopy({ view: "canvas", senderSelected: false, presentation: "generic", selectedColorSlot: null }).title, "Propriedades");
});
