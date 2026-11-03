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
  field("tts.edge_prefix", "text"), field("tts.gtts_prefix", "text"), field("tts.atts_prefix", "text"), field("tts.teto_prefix", "text"),
  { ...field("tts.ttv_voice_id", "select"), options: [
    { value: "kasane-teto", label: "Kasane Teto", languages: ["pt-BR", "en-US"], nativePortuguese: false },
    { value: "hatsune-miku", label: "Hatsune Miku", languages: ["pt-BR", "en-US"], nativePortuguese: false },
    { value: "anna-nyui", label: "Anna Nyui", languages: ["pt-BR", "en-US"], nativePortuguese: false },
    { value: "pururu-purpura", label: "Pururu Púrpura", languages: ["pt-BR"], nativePortuguese: true },
  ] },
  { ...field("tts.ttv_language", "select"), options: [{ value: "pt-BR", label: "Português" }, { value: "en-US", label: "Inglês" }] },
  field("tts.ttv_pitch_semitones", "text"), field("tts.ttv_speech_rate", "number"),
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
    assert.doesNotMatch(html, /data-field-id="tts.voice_channel_id"|Canal de voz preferido/);
  }
});

test("valor vazio informa o padrão real em vez de pedir escolha de voz", () => {
  const html = render({});
  assert.match(html, /Padrão do bot/);
  assert.match(html, /Francisca/);
  assert.match(html, /Português — padrão do bot/);
  assert.doesNotMatch(html, /Selecione uma opção/);
});

test("preserva idioma e prefixos salvos, com TextToVocaloid próprio e sem ATTS na interface", () => {
  const draft = { "tts.language": "pt-BR", "tts.edge_prefix": "!!", "tts.gtts_prefix": "??" };
  const before = structuredClone(draft);
  const html = render(draft);
  assert.match(html, /Português — configuração atual/);
  assert.match(html, /aria-label="tts.edge_prefix"[^>]*value="!!"/); assert.match(html, /aria-label="tts.gtts_prefix"[^>]*value="\?\?"/);
  assert.doesNotMatch(html, /data-field-id="tts.atts_prefix"|Prefixo ATTS|Outros prefixos/);
  assert.match(html, /TextToVocaloid/);
  assert.match(html, /data-field-id="tts.teto_prefix"/);
  assert.deepEqual(draft, before);
});

test("idiomas regionais legados conservam a região e o valor salvo", () => {
  const regionalFields = fields.map(item => item.id === "tts.language" ? { ...item, options: [
    { value: "pt", label: "Português (Brasil)" }, { value: "pt-PT", label: "Português (Portugal)" },
    { value: "zh-CN", label: "Chinês (simplificado)" }, { value: "zh-TW", label: "Chinês (tradicional, Taiwan)" },
  ] } : item);
  for (const [value, label] of [["pt-pt", "Português (Portugal)"], ["zh_tw", "Chinês (tradicional, Taiwan)"]]) {
    const draft = { "tts.language": value };
    const html = renderToStaticMarkup(React.createElement(TtsVoiceSettings, { fields: regionalFields, values: draft, draft, guildOptions: null, onChange() {} }));
    assert.ok(html.includes(`${label} — configuração atual`));
    assert.deepEqual(draft, { "tts.language": value });
  }
});

test("TextToVocaloid usa padrões do servidor e preserva ajustes salvos sem ação pessoal", () => {
  const draft = { "tts.ttv_voice_id": "hatsune-miku", "tts.ttv_language": "en-US", "tts.ttv_pitch_semitones": "-1.5", "tts.ttv_speech_rate": 1.075, "general.bot_prefix": "!" };
  const before = structuredClone(draft);
  const html = render(draft);
  for (const name of ["Kasane Teto", "Hatsune Miku", "Anna Nyui", "Pururu Púrpura"]) assert.ok(html.includes(name));
  assert.match(html, /Voz padrão do servidor/);
  assert.match(html, /data-field-id="tts.ttv_speech_rate"/);
  assert.match(html, /value="107.5"/);
  assert.match(html, /-1,5 semitons/);
  assert.match(html, /!cd/); assert.match(html, /!rcd/);
  assert.doesNotMatch(html, /Salvar minha voz|Preferências da sua conta|tts\.atts_prefix|tts\.voice_channel_id/);
  assert.deepEqual(draft, before);
});
