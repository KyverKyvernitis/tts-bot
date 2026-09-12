import assert from "node:assert/strict";
import test from "node:test";
import { messageEditorDerivedState } from "../src/components/message-editor/messageEditorDerivedState.js";
import type { DashboardFieldDefinition } from "../src/types/dashboard.js";

const field: DashboardFieldDefinition = {
  id: "welcome.public.title",
  label: "Título",
  type: "text",
  scope: "welcome",
  path: "public.title",
};

test("estado derivado bloqueia canvas durante JSON pendente e preserva webhook", () => {
  const state = messageEditorDerivedState({
    sectionId: "welcome",
    editorId: "public",
    draft: { "welcome.webhook.enabled": true },
    jsonDirty: false,
    pendingJsonChanges: true,
    view: "inspector",
    senderSelected: false,
    presentation: "generic",
    selectedColorSlot: null,
    inspectorFields: [field],
    selectedField: field,
    activeTextField: null,
  });
  assert.equal(state.senderEnabled, true);
  assert.equal(state.applyDisabled, true);
  assert.equal(state.canvasInteractive, false);
  assert.equal(state.colorSlotIds, null);
  assert.match(state.contextTitle, /Título|Configura/);
});

test("estado derivado resolve painel de cores pelo editor sem duplicar regra no componente", () => {
  const state = messageEditorDerivedState({
    sectionId: "color_roles",
    editorId: "color-panel:panel-2",
    draft: {
      "color_roles.panel_layout": [
        { id: "panel-1", slots: [1, 2] },
        { id: "panel-2", slots: [11, 12, 13] },
      ],
    },
    jsonDirty: false,
    pendingJsonChanges: false,
    view: "canvas",
    senderSelected: false,
    presentation: "color_panel",
    selectedColorSlot: 12,
    inspectorFields: [],
    selectedField: null,
    activeTextField: null,
  });
  assert.deepEqual(state.colorSlotIds, [11, 12, 13]);
  assert.equal(state.activeColorPanel?.id, "panel-2");
  assert.equal(state.canvasInteractive, true);
});
