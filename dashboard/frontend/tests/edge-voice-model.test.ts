import assert from "node:assert/strict";
import test from "node:test";
import { edgeVoicePickerModel, edgeVoiceLocale, voiceForEdgeLocale } from "../src/components/module-settings/edgeVoiceModel";
import { moduleAreasFor } from "../src/components/module-settings/moduleAreas";
import { ttsSection } from "../../backend/src/config/sections/dashboardTtsSection";

const voices = [
  { value: "pt-BR-FranciscaNeural", label: "Francisca", locale: "pt-BR" },
  { value: "en-US-AriaNeural", label: "Aria", locale: "en-US" },
  { value: "ja-JP-NanamiNeural", label: "Nanami", locale: "ja-JP" },
];
test("idioma vem da voz salva e cada lista contém somente suas vozes", () => {
  const model = edgeVoicePickerModel(voices, "en-US-AriaNeural");
  assert.equal(model.locale, "en-US");
  assert.deepEqual(model.options.map(option => option.value), ["en-US-AriaNeural"]);
  assert.equal(model.languages.length, 3);
  assert.equal(voiceForEdgeLocale(voices, "ja-JP"), "ja-JP-NanamiNeural");
  assert.equal(voiceForEdgeLocale(voices, "fr-FR"), null);
});
test("catálogo ausente preserva a configuração existente e o padrão fica distinto", () => {
  const current = edgeVoicePickerModel([], "en-GB-SoniaNeural");
  assert.equal(current.options[0].value, "en-GB-SoniaNeural");
  assert.equal(current.locale, "en-GB");
  assert.equal(current.missing, true);
  assert.ok(current.languages.some(option => option.value === "pt-BR"), "O padrão continua acessível durante falhas do catálogo");
  const model = edgeVoicePickerModel(voices, "");
  assert.equal(model.options[0].value, "");
  assert.equal(model.options[0].label, "Padrão do bot");
  assert.equal(model.name, "Francisca");
  assert.equal(voiceForEdgeLocale(voices, "pt-BR"), "");
  assert.equal(edgeVoiceLocale("sr-Latn-RS-NicholasNeural"), "sr-Latn-RS");
});
test("TTS possui só Voz e Comportamento e nenhum prefixo fica sem área", () => {
  const areas = moduleAreasFor(ttsSection);
  assert.deepEqual(areas.map(area => area.id), ["voice", "behavior"]);
  for (const field of ttsSection.fields.filter(field => field.id.endsWith("_prefix"))) assert.ok(areas[0].groups.includes(field.group!));
});
