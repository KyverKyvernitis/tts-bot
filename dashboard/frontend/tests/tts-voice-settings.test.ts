import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TtsVoiceSettings } from "../src/components/module-settings/TtsVoiceSettings";
import type { DashboardFieldDefinition } from "../src/types/dashboard";

const field = (id: string, type: DashboardFieldDefinition["type"]): DashboardFieldDefinition => ({ id, label: id, type, scope: "guild", path: id });
const fields = [
  { ...field("tts.voice", "select"), options: [{ value: "pt-BR-AntonioNeural", label: "Antônio" }] },
  { ...field("tts.language", "select"), options: [{ value: "pt", label: "Português" }, { value: "en", label: "Inglês" }] },
  field("tts.rate", "text"), field("tts.pitch", "text"), field("tts.voice_channel_id", "channel"),
  { ...field("tts.engine", "select"), options: [{ value: "edge", label: "Selecionar engine" }] },
];
const render = (draft: Record<string, unknown>) => renderToStaticMarkup(React.createElement(TtsVoiceSettings, { fields, values: draft, draft, guildOptions: null, onChange() {} }));

test("ambos os motores aparecem com seus controles sem depender de engine legada", () => {
  for (const engine of ["edge", "gtts", "android_native", ""]) {
    const html = render({ "tts.engine": engine, "tts.voice": "pt-BR-AntonioNeural", "tts.language": "en", "tts.rate": "+25%", "tts.pitch": "-10Hz" });
    assert.match(html, /Microsoft Edge/); assert.match(html, /Google TTS/);
    assert.match(html, /Antônio/); assert.match(html, /Inglês/);
    assert.match(html, /\+25%/); assert.match(html, /-10Hz/);
    assert.doesNotMatch(html, /data-field-id="tts.engine"/);
    assert.match(html, /data-field-id="tts.voice_channel_id"/);
  }
});

test("valor vazio informa o padrão real em vez de pedir escolha de voz", () => {
  const html = render({});
  assert.match(html, /Francisca — padrão do bot/);
  assert.match(html, /Português — padrão do bot/);
  assert.doesNotMatch(html, /Selecione uma opção/);
});

test("mostra idioma legado e prefixos salvos sem reescrever o rascunho", () => {
  const draft = { "tts.language": "pt-BR", "tts.edge_prefix": "!!", "tts.gtts_prefix": "??" };
  const before = structuredClone(draft);
  const html = render(draft);
  assert.match(html, /Português — configuração atual/);
  assert.match(html, /<code>!!<\/code>/); assert.match(html, /<code>\?\?<\/code>/);
  assert.deepEqual(draft, before);
});
