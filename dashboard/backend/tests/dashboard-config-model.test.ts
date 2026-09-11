import assert from "node:assert/strict";
import test from "node:test";
import { defaultBirthdayDoc, defaultGuildDoc, defaultWelcomeDoc } from "../src/config/dashboardDocumentDefaults.js";
import {
  dashboardCommandContextFromGuild,
  dashboardSummaryFromDocs,
  dashboardValuesFromDocs,
  planDashboardUpdates,
  type DashboardDocs,
} from "../src/services/dashboardConfigModel.js";
import { DashboardConfigValidationError } from "../src/config/dashboardTypes.js";

function docs(guildId = "123456789012345678"): DashboardDocs {
  return {
    guild: defaultGuildDoc(guildId),
    welcome: defaultWelcomeDoc(guildId),
    birthday: defaultBirthdayDoc(guildId),
  };
}

test("modelo produz valores e summary sem depender do MongoDB", () => {
  const state = docs();
  const values = dashboardValuesFromDocs(state);
  assert.equal(values["general.bot_prefix"], "_");
  assert.equal(values["welcome.enabled"], false);
  const summary = dashboardSummaryFromDocs("123", state);
  assert.equal(summary.guildId, "123");
  assert.equal(summary.sections.some((section) => section.id === "welcome"), true);
});

test("contexto de comandos preserva defaults e modo commands", () => {
  const guild = defaultGuildDoc("123");
  guild.gincana_input_mode = "COMMANDS";
  guild.bot_prefix = "!";
  const context = dashboardCommandContextFromGuild(guild);
  assert.equal(context.gamesMode, "commands");
  assert.equal(context.prefixes.bot_prefix, "!");
  assert.equal(context.prefixes.atts_prefix, "%");
});

test("planner sincroniza canal de webhook e marca seção welcome", () => {
  const state = docs();
  const plan = planDashboardUpdates(state, { "welcome.channel_id": "123456789012345679" });
  assert.deepEqual(plan.saved, ["welcome.channel_id"]);
  assert.deepEqual(plan.changedSections, ["welcome"]);
  const welcomePatch = plan.patches.get("welcome") ?? {};
  assert.equal(String(welcomePatch["webhook.channel_id"]), "123456789012345679");
  assert.equal(plan.values["welcome.channel_id"], "123456789012345679");
});

test("planner normaliza layout e mantém panel_count coerente", () => {
  const state = docs();
  const plan = planDashboardUpdates(state, {
    "color_roles.panel_layout": [
      { id: "a", slots: [1, 2] },
      { id: "b", slots: [11, 12] },
    ],
  });
  const patch = plan.patches.get("guild") ?? {};
  assert.equal(patch["color_roles.panel_count"], 2);
  assert.deepEqual(plan.values["color_roles.panel_layout"], [
    { id: "a", slots: [1, 2] },
    { id: "b", slots: [11, 12] },
  ]);
});

test("planner marca birthday quando timezone geral muda", () => {
  const plan = planDashboardUpdates(docs(), { "general.timezone": "UTC" });
  assert.deepEqual(plan.changedSections, ["general", "birthday"]);
  assert.equal(plan.values["general.timezone"], "UTC");
});

test("planner bloqueia ativação sem pré-requisitos", () => {
  assert.throws(
    () => planDashboardUpdates(docs(), { "welcome.enabled": true }),
    (error: unknown) => error instanceof DashboardConfigValidationError
      && error.sectionId === "welcome"
      && error.issues.some((issue) => issue.includes("canal")),
  );
});

test("planner ignora campos desconhecidos e limita lote a 250 entradas", () => {
  const updates: Record<string, unknown> = {};
  for (let index = 0; index < 300; index += 1) updates[`unknown.${index}`] = index;
  updates["general.bot_prefix"] = "!";
  const plan = planDashboardUpdates(docs(), updates);
  assert.deepEqual(plan.saved, []);
  assert.equal(plan.values["general.bot_prefix"], "_");
});
