import assert from "node:assert/strict";
import test from "node:test";
import { defaultBirthdayDoc, defaultGuildDoc, defaultWelcomeDoc } from "../src/config/dashboardDocumentDefaults.js";
import { applyLegacyFeatureFlags } from "../src/config/dashboardLegacyCompat.js";
import { dashboardSections } from "../src/config/dashboardCatalog.js";
import { DashboardConfigValueError } from "../src/config/dashboardTypes.js";
import { dashboardCommandContextFromGuild, dashboardValuesFromDocs, planDashboardUpdates } from "../src/services/dashboardConfigModel.js";

const guildId = "123456789012345678";
const docs = () => ({ guild: defaultGuildDoc(guildId), welcome: defaultWelcomeDoc(guildId), birthday: defaultBirthdayDoc(guildId) });

test("economia expõe as configurações existentes, sem chave geral nem saldos", () => {
  const section = dashboardSections.find((item) => item.id === "economy")!;
  assert.deepEqual(section.fields.map((field) => field.path), ["gincana_channel_id", "gincana_input_mode", "gincana_staff_role_id"]);
  const values = dashboardValuesFromDocs(docs());
  assert.equal(values["economy.input_mode"], "triggers");
  assert.equal(values["economy.channel_id"], "");
});

test("salva canal, modo e staff sem tocar nos saldos e comunica a seção ao bot", () => {
  const state = docs();
  const balance = { player: { chips: 450, bonus: 20 } };
  state.guild.gincana_chips = balance;
  const plan = planDashboardUpdates(state, {
    "economy.channel_id": "123456789012345679",
    "economy.input_mode": "commands",
    "economy.staff_role_id": "123456789012345680",
    "economy.chips": 0,
  });
  assert.deepEqual(plan.changedSections, ["economy"]);
  const patch = plan.patches.get("guild")!;
  assert.deepEqual(Object.keys(patch).sort(), ["gincana_channel_id", "gincana_input_mode", "gincana_staff_role_id"]);
  assert.equal(String(patch.gincana_channel_id), "123456789012345679");
  assert.equal(String(patch.gincana_staff_role_id), "123456789012345680");
  assert.equal(plan.values["economy.staff_role_id"], "123456789012345680");
  assert.equal(state.guild.gincana_chips, balance);
  assert.equal(dashboardCommandContextFromGuild(state.guild).gamesMode, "commands");
});

test("cargos legados são preservados, inclusive remoção explícita do cargo", () => {
  const state = docs();
  applyLegacyFeatureFlags("guild", { anti_mzk_staff_role_id: "123456789012345681", gincana_input_mode: "COMMANDS" }, state.guild);
  assert.equal(dashboardValuesFromDocs(state)["economy.staff_role_id"], "123456789012345681");
  assert.equal(dashboardValuesFromDocs(state)["economy.input_mode"], "commands");
  const plan = planDashboardUpdates(state, { "economy.staff_role_id": "", "economy.channel_id": "", "economy.input_mode": "invalid" });
  assert.equal(plan.values["economy.staff_role_id"], "");
  assert.equal(plan.values["economy.channel_id"], "");
  assert.equal(plan.values["economy.input_mode"], "triggers");
  const loaded = { ...state.guild };
  applyLegacyFeatureFlags("guild", { ...state.guild, anti_mzk_staff_role_id: "123456789012345681" }, loaded);
  assert.equal(loaded.gincana_staff_role_id, state.guild.gincana_staff_role_id);
});

test("cargo padrão do servidor não pode virar staff da economia", () => {
  assert.throws(() => planDashboardUpdates(docs(), { "economy.staff_role_id": guildId }),
    (error: unknown) => error instanceof DashboardConfigValueError && error.fieldId === "economy.staff_role_id");
});
