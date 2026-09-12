import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardFieldDefinition } from "../src/types/dashboard";
import {
  cloneMessageEditorValue,
  messageEditorTextLimitNotice,
  messageEditorValuesEqual,
  messageEditorVisualFieldVisible,
  prefixMessageEditorTextLines,
  relatedMessageEditorContextFields,
  replaceMessageEditorText,
  wrapMessageEditorText,
} from "../src/components/message-editor/messageEditorModel";
import {
  fieldString,
  filterPreviewFields,
  needsLightOutline,
  resolvePreviewRenderPlan,
  resolveSender,
} from "../src/components/message-editor/messagePreviewModel";
import { messageEditorFieldGroups, messageEditorHasFieldChanges } from "../src/components/message-editor/messageEditorFields";

const field = (id: string, type: DashboardFieldDefinition["type"] = "text"): DashboardFieldDefinition => ({
  id,
  label: id,
  type,
  scope: "guild",
  path: id,
});

test("clona valores estruturados e compara sem compartilhar referências", () => {
  const source = { nested: { value: 1 }, items: ["a"] };
  const clone = cloneMessageEditorValue(source);
  assert.ok(messageEditorValuesEqual(source, clone));
  assert.notEqual(clone, source);
  assert.notEqual(clone.nested, source.nested);
  clone.nested.value = 2;
  assert.equal(source.nested.value, 1);
  assert.equal(messageEditorValuesEqual(source, clone), false);
});

test("oculta configuração de webhook dependente quando webhook está desligado", () => {
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.webhook.enabled", {}), true);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.webhook.name", {}), false);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.webhook.name", {
    "welcome.webhook.enabled": true,
    "welcome.webhook.name_mode": "fixed",
  }), true);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.webhook.name", {
    "welcome.webhook.enabled": true,
    "welcome.webhook.name_mode": "server",
  }), false);
});

test("respeita campos exclusivos dos modos visual components-v2 e embed", () => {
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.media_url", {
    "welcome.render_mode": "components_v2",
    "welcome.media_mode": "custom",
  }), true);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.media_url", {
    "welcome.render_mode": "embed",
    "welcome.media_mode": "custom",
  }), false);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.embed.image_url", {
    "welcome.render_mode": "embed",
    "welcome.embed.image_mode": "custom",
  }), true);
  assert.equal(messageEditorVisualFieldVisible("welcome-public", "welcome.embed.image_url", {
    "welcome.render_mode": "normal",
    "welcome.embed.image_mode": "custom",
  }), false);
});

test("agrupa campos contextuais sem incluir controles não relacionados", () => {
  const fields = [
    field("welcome.embed.author_name"),
    field("welcome.embed.author_icon_mode", "select"),
    field("welcome.embed.author_icon_url", "url"),
    field("welcome.embed.footer_text"),
  ];
  assert.deepEqual(
    relatedMessageEditorContextFields(fields[0], fields).map((item) => item.id),
    ["welcome.embed.author_name", "welcome.embed.author_icon_mode", "welcome.embed.author_icon_url"],
  );
  assert.deepEqual(relatedMessageEditorContextFields(null, fields), []);
});


test("limite das ferramentas de texto preserva mensagem e aceita valores no teto", () => {
  const limited = { ...field("body", "textarea"), maxLength: 5 };
  assert.equal(messageEditorTextLimitNotice(limited, "12345"), null);
  assert.equal(messageEditorTextLimitNotice(limited, "123456"), "Limite de 5 caracteres");
  assert.equal(messageEditorTextLimitNotice(field("body", "textarea"), "x".repeat(1000)), null);
});

test("substitui seleção e preserva cursor previsto", () => {
  assert.deepEqual(replaceMessageEditorText("abc", "X", { start: 1, end: 2 }), {
    value: "aXc", start: 2, end: 2,
  });
  assert.deepEqual(replaceMessageEditorText("abc", "XY", { start: 1, end: 2 }, true), {
    value: "aXYc", start: 1, end: 3,
  });
});

test("envolve seleção e usa placeholder quando não há seleção", () => {
  assert.deepEqual(wrapMessageEditorText("abc", "**", "**", "texto", { start: 1, end: 2 }), {
    value: "a**b**c", start: 3, end: 4,
  });
  assert.deepEqual(wrapMessageEditorText("", "`", "`", "código", null), {
    value: "`código`", start: 1, end: 7,
  });
});

test("prefixa todas as linhas selecionadas", () => {
  assert.deepEqual(prefixMessageEditorTextLines("a\nb", "> ", "texto", { start: 0, end: 3 }), {
    value: "> a\n> b", start: 2, end: 7,
  });
});

test("resolve remetente padrão e webhook sem confundir avatar do servidor", () => {
  const senderFields = [field("welcome.webhook.enabled", "boolean")];
  assert.deepEqual(resolveSender({
    senderFields: [],
    draft: {},
    botName: "Osaka",
    botAvatarUrl: "bot.png",
    guildName: "Guild",
    guildAvatarUrl: "guild.png",
  }), { enabled: false, name: "Osaka", avatar: "bot.png", badge: "BOT" });

  assert.deepEqual(resolveSender({
    senderFields,
    draft: {
      "welcome.webhook.enabled": true,
      "welcome.webhook.name_mode": "server",
      "welcome.webhook.avatar_mode": "server",
    },
    botName: "Osaka",
    botAvatarUrl: "bot.png",
    guildName: "Guild",
    guildAvatarUrl: "guild.png",
  }), { enabled: true, name: "Guild", avatar: "guild.png", badge: "APP" });
});

test("plano adaptativo de preview filtra campos conforme o modo", () => {
  const fields = [
    field("welcome.public.title"),
    field("welcome.public.footer"),
    field("welcome.embed.title"),
    field("welcome.dm.title"),
  ];
  const embedPlan = resolvePreviewRenderPlan("adaptive", "welcome", "welcome-public", { "welcome.render_mode": "embed" });
  assert.equal(embedPlan.renderKind, "embed");
  assert.deepEqual(filterPreviewFields(fields, embedPlan).map((item) => item.id), ["welcome.embed.title"]);

  const normalPlan = resolvePreviewRenderPlan("adaptive", "welcome", "welcome-public", { "welcome.render_mode": "normal" });
  assert.deepEqual(filterPreviewFields(fields, normalPlan).map((item) => item.id), ["welcome.public.title", "welcome.dm.title"]);
});

test("converte valores de preview e detecta cores que exigem contorno claro", () => {
  assert.equal(fieldString(field("x"), { x: 0 }), "0");
  assert.equal(fieldString(field("x"), { x: null }), "");
  assert.equal(needsLightOutline("#000000"), true);
  assert.equal(needsLightOutline("#ffffff"), false);
  assert.equal(needsLightOutline("invalid"), false);
});


test("agrupa campos visuais, remetente e JSON sem mudar a ordem", () => {
  const fields = [
    { id: "welcome.webhook.enabled", path: "x", label: "Webhook", type: "boolean" },
    { id: "welcome.webhook.name", path: "x", label: "Nome", type: "text" },
    { id: "message.text", path: "x", label: "Texto", type: "textarea" },
    { id: "colors", path: "x", label: "Cores", type: "color_slots" },
  ] as any;
  const grouped = messageEditorFieldGroups(
    fields,
    ["welcome.webhook.enabled", "welcome.webhook.name"],
    "welcome-public",
    { "welcome.webhook.enabled": false },
  );
  assert.deepEqual(grouped.jsonFields.map((field) => field.id), ["welcome.webhook.enabled", "welcome.webhook.name", "message.text"]);
  assert.deepEqual(grouped.visualFields.map((field) => field.id), ["welcome.webhook.enabled", "message.text", "colors"]);
  assert.deepEqual(grouped.senderFields.map((field) => field.id), ["welcome.webhook.enabled"]);
  assert.deepEqual(grouped.messageFields.map((field) => field.id), ["message.text", "colors"]);
});

test("detecta alterações apenas nos campos do editor", () => {
  const fields = [{ id: "message.text", path: "x", label: "Texto", type: "textarea" }] as any;
  assert.equal(messageEditorHasFieldChanges(fields, { "message.text": "a", other: 1 }, { "message.text": "a", other: 2 }), false);
  assert.equal(messageEditorHasFieldChanges(fields, { "message.text": "a" }, { "message.text": "b" }), true);
});
