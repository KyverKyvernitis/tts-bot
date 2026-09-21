import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { dashboardFieldError, dashboardChangedFieldErrors, reconcileSavedDraft } from "../src/app/dashboardFormValidation";
import { birthdayTime, moduleAreasFor, parseBirthdayTime } from "../src/components/module-settings/moduleAreas";
import { MessageGroupPanel } from "../src/components/SectionEditorPanels";
import { DashboardFieldControl } from "../src/components/DashboardFieldControl";
import { FieldFeedbackContext } from "../src/components/module-settings/FieldFeedback";
import { messageEditorFieldGroups } from "../src/components/message-editor/messageEditorFields";
import type { DashboardFieldDefinition, DashboardSectionDefinition } from "../src/types/dashboard";

const field = (id: string, type: DashboardFieldDefinition["type"], extra: Partial<DashboardFieldDefinition> = {}): DashboardFieldDefinition => ({ id, label: id, type, scope: "guild", path: id, ...extra });

test("erro permite corrigir URL mesmo quando sua opção de exibição está oculta", () => {
  const url = field("welcome.media_url", "url");
  const draft = { "welcome.media_mode": "none" };
  const revealed = messageEditorFieldGroups([url], [], "welcome-entry", draft, url.id);
  assert.equal(revealed.visualFields.some(item => item.id === url.id), true);
});

test("valida o número vazio sem confundir com zero e respeita os limites", () => {
  const hour = field("birthday.announce_hour", "number", { min: 0, max: 23 });
  for (const value of ["", null, undefined, NaN, Infinity, "inválido", -1, 24]) assert.ok(dashboardFieldError(hour, value));
  for (const value of [0, 23, "12"]) assert.equal(dashboardFieldError(hour, value), null);
});

test("URLs aceitam os formatos legados e rejeitam protocolos não suportados", () => {
  const url = field("welcome.image_url", "url");
  for (const value of ["", "https://example.com/p.png", "<https://example.com/a?b=1&amp;c=2>", "'http://example.com'"]) assert.equal(dashboardFieldError(url, value), null);
  for (const value of ["imagem.png", "javascript:alert(1)", "data:image/png;base64,x"]) assert.ok(dashboardFieldError(url, value));
});

test("somente os campos enviados geram erros e mantêm o nome do campo", () => {
  const title = field("forms.panel.title", "text", { maxLength: 5 });
  assert.deepEqual(dashboardChangedFieldErrors([title], { [title.id]: "123456", "unused.url": "inválida" }), { [title.id]: "Use até 5 caracteres." });
  assert.deepEqual(dashboardChangedFieldErrors([title], { [title.id]: "12345" }), {});
});

test("retorno do servidor normaliza o que foi salvo e preserva novas edições", () => {
  const submitted = { text: "Enviado", channel: "", slots: { "1": { name: "Azul" } }, hidden: false };
  const current = { ...submitted, text: "Ainda digitando", slots: { "1": { name: "Verde" } } };
  const saved = { ...submitted, channel: "123", hidden: true, revision: 2 };
  const result = reconcileSavedDraft(submitted, current, saved);
  assert.deepEqual(result, { ...saved, text: "Ainda digitando", slots: { "1": { name: "Verde" } } });
  assert.equal(submitted.text, "Enviado");
  assert.deepEqual(reconcileSavedDraft(submitted, structuredClone(submitted), saved), saved);
});

test("horário combinado usa hora e minuto existentes, inclusive meia-noite", () => {
  for (const value of ["00:00", "09:05", "23:59"]) {
    const time = parseBirthdayTime(value)!;
    assert.ok(time);
    assert.equal(birthdayTime({ "birthday.announce_hour": time[0], "birthday.announce_minute": time[1] }), value);
  }
  for (const value of ["", "24:00", "9:00", "12:60", "12:00:30"]) assert.equal(parseBirthdayTime(value), null);
  assert.equal(birthdayTime({}), "09:00");
  assert.equal(birthdayTime({ "birthday.announce_hour": 25 }), "");
});

test("áreas preservam grupos novos e não repetem a ativação do cabeçalho", () => {
  const section: DashboardSectionDefinition = { id: "forms", label: "", emoji: "", description: "", fields: [
    field("enabled", "boolean", { group: "Ativação" }), field("channel", "channel", { group: "Canais" }),
    field("title", "text", { group: "Painel" }), field("questions", "form_fields", { group: "Perguntas" }),
    field("future", "text", { group: "Novo grupo" }), field("ungrouped", "text"),
  ] };
  const areas = moduleAreasFor(section);
  assert.deepEqual(areas[0].groups, ["Canais", "Painel"]);
  const groups = areas.flatMap(area => area.groups);
  assert.equal(groups.length, new Set(groups).size);
  assert.deepEqual(new Set(groups), new Set(["Canais", "Painel", "Perguntas", "Novo grupo", "Geral"]));
});

test("mensagens privadas podem ser preparadas antes do envio ser habilitado", () => {
  const content = field("welcome.dm.message", "textarea", { group: "Mensagem privada" });
  const html = renderToStaticMarkup(React.createElement(MessageGroupPanel, {
    sectionId: "welcome", group: "Mensagem privada", fields: [content],
    metadata: { kind: "message", editors: [{ id: "welcome-dm", label: "Mensagem privada", fieldIds: [content.id] }] },
    values: {}, draft: { [content.id]: "Conteúdo ainda em rascunho", "welcome.dm.enabled": false },
    guildOptions: null, renderFields: () => null, onOpenEditor() {},
  }));
  assert.match(html, /Conteúdo ainda em rascunho/);
  assert.match(html, />Editar<\/button>/);
  assert.doesNotMatch(html, /disabled|Mensagem editável/);
  assert.match(html, /aria-expanded="false"/);
});

test("falha das opções preserva a seleção e oferece recuperar a lista", () => {
  const html = renderToStaticMarkup(React.createElement(FieldFeedbackContext.Provider, {
    value: { errors: {}, onRetryOptions() {} }, children: React.createElement(DashboardFieldControl, {
      field: field("welcome.channel_id", "channel"), value: "123456789012345678", guildOptions: null, onChange() {},
    }),
  }));
  assert.match(html, /Opções indisponíveis/);
  assert.match(html, /A seleção atual está preservada/);
  assert.match(html, /Tentar novamente/);
  assert.doesNotMatch(html, /ID do canal/);
});

test("erro de campo associa mensagem e controle para leitores de tela", () => {
  const item = field("tts.speech_limit_seconds", "number", { min: 1 });
  const html = renderToStaticMarkup(React.createElement(FieldFeedbackContext.Provider, {
    value: { errors: { [item.id]: "O mínimo é 1." } }, children: React.createElement(DashboardFieldControl, { field: item, value: "", guildOptions: null, onChange() {} }),
  }));
  assert.match(html, /aria-invalid="true"/);
  assert.match(html, /aria-describedby="[^"]+"/);
  assert.match(html, /role="alert">O mínimo é 1\./);
});
