import assert from "node:assert/strict";
import test from "node:test";
import { ttsSection } from "../src/config/sections/dashboardTtsSection.js";
import { defaultGuildDoc, defaultWelcomeDoc, defaultBirthdayDoc } from "../src/config/dashboardDocumentDefaults.js";
import { dashboardValuesFromDocs, planDashboardUpdates } from "../src/services/dashboardConfigModel.js";

const guildId = "123456789012345678";
const docs = () => ({ guild: defaultGuildDoc(guildId), welcome: defaultWelcomeDoc(guildId), birthday: defaultBirthdayDoc(guildId) });

test("catálogo de TTS oferece ajustes dos dois motores sem seletor exclusivo", () => {
  const ids = new Set(ttsSection.fields.map(field => field.id));
  assert.equal(ids.has("tts.engine"), false);
  for (const id of ["tts.voice", "tts.rate", "tts.pitch", "tts.language"]) assert.ok(ids.has(id));
});

test("salvar gTTS mantém voz, velocidade e tom do Edge e vice-versa", () => {
  const state = docs();
  planDashboardUpdates(state, { "tts.voice": "pt-BR-AntonioNeural", "tts.rate": "+25%", "tts.pitch": "-10Hz" });
  planDashboardUpdates(state, { "tts.language": "en" });
  let loaded = dashboardValuesFromDocs(state);
  assert.equal(loaded["tts.voice"], "pt-BR-AntonioNeural");
  assert.equal(loaded["tts.rate"], "+25%");
  assert.equal(loaded["tts.pitch"], "-10Hz");
  assert.equal(loaded["tts.language"], "en");
  planDashboardUpdates(state, { "tts.voice": "pt-BR-FranciscaNeural" });
  loaded = dashboardValuesFromDocs(state);
  assert.equal(loaded["tts.language"], "en");
  assert.equal(loaded["tts.voice"], "pt-BR-FranciscaNeural");
});

test("pedido de cliente antigo não altera a engine legada do bot", () => {
  const state = docs();
  (state.guild.tts_defaults as Record<string, unknown>).engine = "gtts";
  const plan = planDashboardUpdates(state, { "tts.engine": "edge", "tts.rate": "+10%" });
  assert.deepEqual(plan.saved, ["tts.rate"]);
  assert.equal((state.guild.tts_defaults as Record<string, unknown>).engine, "gtts");
  assert.deepEqual(plan.patches.get("guild"), { "tts_defaults.rate": "+10%" });
});

test("padrões dos motores podem ser restaurados independentemente", () => {
  const state = docs();
  planDashboardUpdates(state, { "tts.voice": "pt-BR-AntonioNeural", "tts.language": "fr" });
  let plan = planDashboardUpdates(state, { "tts.voice": "" });
  assert.equal(plan.values["tts.voice"], "");
  assert.equal(plan.values["tts.language"], "fr");
  plan = planDashboardUpdates(state, { "tts.language": "" });
  assert.equal(plan.values["tts.language"], "");
});

test("idiomas enviados pelo painel usam códigos aceitos pelo gTTS", () => {
  const language = ttsSection.fields.find(field => field.id === "tts.language")!;
  const supported = new Set(["", "pt", "en", "es", "fr", "de", "it", "ja", "ko"]);
  for (const option of language.options!) {
    assert.ok(supported.has(option.value), option.value);
    assert.equal(planDashboardUpdates(docs(), { "tts.language": option.value }).values["tts.language"], option.value);
  }
});
