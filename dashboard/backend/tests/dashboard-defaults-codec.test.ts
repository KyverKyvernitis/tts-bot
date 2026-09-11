import assert from "node:assert/strict";
import test from "node:test";
import {
  defaultBirthdayDoc,
  defaultColorPanelLayout,
  defaultColorSlots,
  defaultFormsConfig,
  defaultGuildDoc,
  defaultWelcomeDoc,
} from "../src/config/dashboardDocumentDefaults.js";
import {
  defaultColorPanelLayout as defaultColorPanelLayoutDomain,
  defaultColorSlots as defaultColorSlotsDomain,
} from "../src/config/dashboardColorRoleDefaults.js";
import { defaultFormsConfig as defaultFormsConfigDomain } from "../src/config/dashboardFormsDefaults.js";
import { defaultGuildDoc as defaultGuildDocDomain } from "../src/config/dashboardGuildDefaults.js";
import {
  defaultBirthdayDoc as defaultBirthdayDocDomain,
  defaultWelcomeDoc as defaultWelcomeDocDomain,
} from "../src/config/dashboardFeatureDefaults.js";
import {
  normalizeColorPanelLayout,
  normalizeFieldValue,
  serializeFieldValue,
} from "../src/config/dashboardValueCodec.js";
import { serializeSnowflake } from "../src/config/dashboardSnowflakes.js";
import type { DashboardFieldDefinition } from "../src/config/dashboardTypes.js";

function field(type: DashboardFieldDefinition["type"], overrides: Partial<DashboardFieldDefinition> = {}): DashboardFieldDefinition {
  return {
    id: "test.field",
    label: "Teste",
    type,
    scope: "guild",
    path: "test.field",
    ...overrides,
  };
}

test("fachada de defaults preserva exatamente os módulos por domínio", () => {
  const guildId = "123456789012345678";
  assert.deepEqual(defaultColorPanelLayout(), defaultColorPanelLayoutDomain());
  assert.deepEqual(defaultColorSlots(), defaultColorSlotsDomain());
  assert.deepEqual(defaultFormsConfig(), defaultFormsConfigDomain());
  assert.deepEqual(defaultGuildDoc(guildId), defaultGuildDocDomain(guildId));
  assert.deepEqual(defaultWelcomeDoc(guildId), defaultWelcomeDocDomain(guildId));
  assert.deepEqual(defaultBirthdayDoc(guildId), defaultBirthdayDocDomain(guildId));
});

test("defaults retornam estruturas independentes e preservam snowflake do servidor", () => {
  const first = defaultFormsConfig();
  const second = defaultFormsConfig();
  first.modal.fields[0].label = "alterado";
  assert.equal(second.modal.fields[0].label, "Nome");

  const slotsA = defaultColorSlots() as Record<string, Record<string, unknown>>;
  const slotsB = defaultColorSlots() as Record<string, Record<string, unknown>>;
  slotsA["1"].name = "alterado";
  assert.equal(slotsB["1"].name, "Vermelho escuro");

  const guildId = "123456789012345678";
  const guild = defaultGuildDoc(guildId);
  assert.equal(serializeSnowflake(guild.guild_id), guildId);
});

test("codec normaliza formulário sem ultrapassar limites do Discord", () => {
  const normalized = normalizeFieldValue(field("form_fields"), [
    { label: " X ".repeat(40), placeholder: "p".repeat(150), long: false, min_length: 999, max_length: 999 },
    { label: "Longa", long: true, min_length: -5, max_length: 5000, required: false },
  ]) as Array<Record<string, unknown>>;

  assert.equal(normalized.length, 2);
  assert.equal(String(normalized[0].label).length <= 45, true);
  assert.equal(String(normalized[0].placeholder).length, 100);
  assert.equal(normalized[0].min_length, 120);
  assert.equal(normalized[0].max_length, 120);
  assert.equal(normalized[1].min_length, 0);
  assert.equal(normalized[1].max_length, 1000);
  assert.equal(normalized[1].required, false);
});

test("codec de painéis remove slots inválidos, duplicados e limita quantidade", () => {
  const normalized = normalizeColorPanelLayout([
    { id: "cores", slots: [1, 1, 2, 31, 0, 3, 4, 5, 6, 7, 8, 9, 10, 11] },
    { id: "cores", slots: [11, 12] },
    { id: "terceiro", slots: [30] },
    { id: "ignorado", slots: [20] },
  ]);

  assert.deepEqual(normalized[0], { id: "cores", slots: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] });
  assert.deepEqual(normalized[1], { id: "panel-2", slots: [11, 12] });
  assert.deepEqual(normalized[2], { id: "terceiro", slots: [30] });
});

test("codec preserva regras de URL, listas, selects e serialização", () => {
  assert.equal(normalizeFieldValue(field("url"), "<https://example.com/a?x=1&amp;y=2>"), "https://example.com/a?x=1&y=2");
  assert.equal(normalizeFieldValue(field("url"), "javascript:alert(1)"), "");
  assert.deepEqual(normalizeFieldValue(field("string_list"), " a\na\n b \n"), ["a", "b"]);
  assert.equal(normalizeFieldValue(field("select", { options: [{ value: "a", label: "A" }, { value: "b", label: "B" }] }), "x"), "a");

  const role = field("role");
  assert.equal(serializeFieldValue(role, "123456789012345678"), "123456789012345678");
});
