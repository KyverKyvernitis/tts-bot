import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardFieldDefinition } from "../src/types/dashboard";
import {
  createLegacyMessageEditor,
  sectionEditorEnabled,
  sectionEditorFieldVisible,
  sectionEditorValuesEqual,
} from "../src/components/sectionEditorModel";

const field = (id: string, type: DashboardFieldDefinition["type"] = "text"): DashboardFieldDefinition => ({
  id,
  label: id,
  type,
  scope: "guild",
  path: id,
});

test("compara valores estruturados sem depender de identidade", () => {
  assert.equal(sectionEditorValuesEqual({ a: [1, 2] }, { a: [1, 2] }), true);
  assert.equal(sectionEditorValuesEqual({ a: 1 }, { a: 2 }), false);
});

test("habilitação de editores preserva dependências funcionais", () => {
  assert.equal(sectionEditorEnabled("welcome", "Mensagem de entrada", { "welcome.enabled": false }), false);
  assert.equal(sectionEditorEnabled("welcome", "Mensagem de entrada", { "welcome.enabled": true }), true);
  assert.equal(sectionEditorEnabled("welcome", "Mensagem privada", { "welcome.enabled": true, "welcome.dm_enabled": false }), false);
  assert.equal(sectionEditorEnabled("welcome", "Mensagem privada", { "welcome.enabled": true, "welcome.dm_enabled": true }), true);
  assert.equal(sectionEditorEnabled("forms", "Aprovação", { "forms.approval.enabled": false }), false);
  assert.equal(sectionEditorEnabled("tickets", "Mensagens", {}), true);
});

test("Edge e gTTS permanecem configuráveis juntos, sem escolha exclusiva de motor", () => {
  assert.equal(sectionEditorFieldVisible("tts", field("tts.ignored_tts_role_enabled", "boolean"), {}), false);
  assert.equal(sectionEditorFieldVisible("tts", field("tts.language", "select"), { "tts.engine": "gtts" }), true);
  assert.equal(sectionEditorFieldVisible("tts", field("tts.language", "select"), { "tts.engine": "edge" }), true);
  assert.equal(sectionEditorFieldVisible("tts", field("tts.voice", "select"), { "tts.engine": "edge" }), true);
  assert.equal(sectionEditorFieldVisible("tts", field("tts.pitch", "text"), { "tts.engine": "gtts" }), true);
  assert.equal(sectionEditorFieldVisible("tts", field("tts.engine", "select"), {}), false);
});

test("visibilidade welcome respeita render, mídia e webhook", () => {
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.style", "select"), { "welcome.render_mode": "components_v2" }), true);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.style", "select"), { "welcome.render_mode": "embed" }), false);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.media_url", "url"), { "welcome.media_mode": "custom" }), true);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.media_url", "url"), { "welcome.media_mode": "none" }), false);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.webhook.name"), { "welcome.webhook.enabled": false }), false);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.webhook.name"), { "welcome.webhook.enabled": true, "welcome.webhook.name_mode": "fixed" }), true);
  assert.equal(sectionEditorFieldVisible("welcome", field("welcome.webhook.avatar_url", "url"), { "welcome.webhook.enabled": true, "welcome.webhook.avatar_mode": "server" }), false);
});

test("aprovação permite preparar campos antes de habilitar o envio", () => {
  const role = field("forms.approval.role_id", "role");
  assert.equal(sectionEditorFieldVisible("forms", role, { "forms.approval.enabled": false }), true);
  assert.equal(sectionEditorFieldVisible("forms", role, { "forms.approval.enabled": true }), true);
  assert.equal(sectionEditorFieldVisible("forms", field("forms.approval.enabled", "boolean"), {}), true);
});

test("editor legado mantém ids e slug estáveis", () => {
  const legacy = createLegacyMessageEditor("Mensagem Privada", [field("a"), field("b")]);
  assert.equal(legacy.id, "legacy-mensagem-privada");
  assert.equal(legacy.label, "Mensagem Privada");
  assert.deepEqual(legacy.fieldIds, ["a", "b"]);
});

test("resolve editor compõe campos, baseline e fallback de variáveis sem mutar a seção", async () => {
  const { resolveSectionMessageEditor } = await import("../src/components/sectionEditorModel");
  const a = field("a");
  const b = field("b");
  const section = {
    id: "welcome",
    label: "Welcome",
    emoji: "👋",
    description: "x",
    fields: [a, b],
  };
  const variables = { syntax: "curly" as const, items: [{ key: "user", label: "Usuário" }] };
  const resolved = resolveSectionMessageEditor(section, {
    id: "message",
    label: "Mensagem",
    fieldIds: ["a"],
  }, { a: "antes", b: "fora" }, variables);
  assert.equal(resolved?.id, "message");
  assert.deepEqual(resolved?.fields.map((item) => item.id), ["a"]);
  assert.deepEqual(resolved?.baseline, { a: "antes" });
  assert.equal(resolved?.variables, variables);
  assert.deepEqual(section.fields.map((item) => item.id), ["a", "b"]);
});

test("resolve editor de painel de cores inclui slots e ignora editor sem campos válidos", async () => {
  const { resolveSectionMessageEditor } = await import("../src/components/sectionEditorModel");
  const slots = field("color_roles.slots", "color_slots");
  const title = field("color_roles.panel_1.title");
  const section = {
    id: "color_roles",
    label: "Cores",
    emoji: "🎨",
    description: "x",
    fields: [slots, title],
  };
  const resolved = resolveSectionMessageEditor(section, {
    id: "color-panel-1",
    label: "Painel 1",
    fieldIds: [title.id],
  }, { [slots.id]: { "1": {} }, [title.id]: "Título" });
  assert.deepEqual(resolved?.fields.map((item) => item.id), [title.id, slots.id]);
  assert.equal(resolveSectionMessageEditor(section, { id: "missing", label: "X", fieldIds: ["nope"] }, {}), null);
});
