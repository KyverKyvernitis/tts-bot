import assert from "node:assert/strict";
import test from "node:test";
import { ttsSection } from "../src/config/sections/dashboardTtsSection.js";
import { defaultGuildDoc, defaultWelcomeDoc, defaultBirthdayDoc } from "../src/config/dashboardDocumentDefaults.js";
import { dashboardValuesFromDocs, planDashboardUpdates } from "../src/services/dashboardConfigModel.js";

const guildId = "123456789012345678";
const docs = () => ({ guild: defaultGuildDoc(guildId), welcome: defaultWelcomeDoc(guildId), birthday: defaultBirthdayDoc(guildId) });
const voices = ["pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "en-US-AriaNeural", "ja-JP-NanamiNeural"];

test("catálogo de TTS oferece ajustes dos dois motores sem seletor exclusivo", () => {
  const ids = new Set(ttsSection.fields.map(field => field.id));
  assert.equal(ids.has("tts.engine"), false);
  for (const id of ["tts.voice", "tts.rate", "tts.pitch", "tts.language"]) assert.ok(ids.has(id));
});

test("salvar gTTS mantém voz, velocidade e tom do Edge e vice-versa", () => {
  const state = docs();
  planDashboardUpdates(state, { "tts.voice": "pt-BR-AntonioNeural", "tts.rate": "+25%", "tts.pitch": "-10Hz" }, voices);
  planDashboardUpdates(state, { "tts.language": "en" });
  let loaded = dashboardValuesFromDocs(state);
  assert.equal(loaded["tts.voice"], "pt-BR-AntonioNeural");
  assert.equal(loaded["tts.rate"], "+25%");
  assert.equal(loaded["tts.pitch"], "-10Hz");
  assert.equal(loaded["tts.language"], "en");
  planDashboardUpdates(state, { "tts.voice": "pt-BR-FranciscaNeural" }, voices);
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
  planDashboardUpdates(state, { "tts.voice": "pt-BR-AntonioNeural", "tts.language": "fr" }, voices);
  let plan = planDashboardUpdates(state, { "tts.voice": "" });
  assert.equal(plan.values["tts.voice"], "");
  assert.equal(plan.values["tts.language"], "fr");
  plan = planDashboardUpdates(state, { "tts.language": "" });
  assert.equal(plan.values["tts.language"], "");
});

test("vozes estrangeiras do catálogo são salvas sem alterar o idioma do Google", () => {
  const state = docs();
  planDashboardUpdates(state, { "tts.language": "pt", "tts.voice": "en-US-AriaNeural" }, voices);
  assert.equal(dashboardValuesFromDocs(state)["tts.voice"], "en-US-AriaNeural");
  const plan = planDashboardUpdates(state, { "tts.voice": "ja-JP-NanamiNeural" }, voices);
  assert.equal(plan.values["tts.voice"], "ja-JP-NanamiNeural");
  assert.equal(plan.values["tts.language"], "pt");
});

test("voz ausente é rejeitada antes de alterar valores, sem virar padrão", () => {
  const state = docs();
  const before = dashboardValuesFromDocs(state);
  assert.throws(() => planDashboardUpdates(state, { "tts.rate": "+50%", "tts.voice": "en-US-InventedNeural" }, voices), /não está no catálogo/);
  assert.deepEqual(dashboardValuesFromDocs(state), before);
});

test("falha do catálogo mantém a voz atual e permite restaurar o padrão", () => {
  const state = docs();
  planDashboardUpdates(state, { "tts.voice": "en-US-AriaNeural" }, voices);
  assert.equal(planDashboardUpdates(state, { "tts.voice": "en-US-AriaNeural", "tts.edge_prefix": "!!" }).values["tts.voice"], "en-US-AriaNeural");
  assert.equal(planDashboardUpdates(state, { "tts.voice": "" }).values["tts.voice"], "");
});

test("todos os prefixos permanecem na área de voz e mantêm validação", () => {
  for (const field of ttsSection.fields.filter(field => field.id.endsWith("_prefix"))) assert.equal(field.group, "Voz");
  const plan = planDashboardUpdates(docs(), { "tts.edge_prefix": "!!", "tts.gtts_prefix": "??", "tts.atts_prefix": "%%", "tts.teto_prefix": "''" });
  assert.equal(plan.values["tts.edge_prefix"], "!!");
  assert.equal(plan.values["tts.gtts_prefix"], "??");
  assert.throws(() => planDashboardUpdates(docs(), { "tts.edge_prefix": "." }));
});

test("painel oferece todos os idiomas do gTTS 2.5.4 e salva os códigos sem alterações", () => {
  const language = ttsSection.fields.find(field => field.id === "tts.language")!;
  // Contract from the pinned provider's tts_langs(), including its extra aliases.
  const supported = "af am ar bg bn bs ca cs cy da de el en es et eu fi fr fr-CA gl gu ha hi hr hu id is it iw ja jw km kn ko la lt lv ml mr ms my ne nl no pa pl pt pt-PT ro ru si sk sq sr su sv sw ta te th tl tr uk ur vi yue zh-CN zh-TW zh".split(" ");
  assert.deepEqual(language.options!.map(option => option.value).sort(), ["", ...supported].sort());
  const state = docs();
  planDashboardUpdates(state, { "tts.voice": "ja-JP-NanamiNeural", "tts.rate": "+25%" }, voices);
  for (const option of language.options!) {
    planDashboardUpdates(state, { "tts.language": option.value });
    const loaded = dashboardValuesFromDocs(state);
    assert.equal(loaded["tts.language"], option.value);
    assert.equal(loaded["tts.voice"], "ja-JP-NanamiNeural");
    assert.equal(loaded["tts.rate"], "+25%");
  }
});
