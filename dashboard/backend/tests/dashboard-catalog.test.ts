import assert from "node:assert/strict";
import test from "node:test";
import { allDashboardFields, dashboardSections } from "../src/config/dashboardCatalog.js";
import { isConfiguredValue, resolveDashboardSectionState } from "../src/config/dashboardSectionState.js";

test("catálogo mantém IDs de seções e campos únicos", () => {
  const sectionIds = dashboardSections.map((section) => section.id);
  const fields = allDashboardFields();
  const fieldIds = fields.map((field) => field.id);

  assert.ok(sectionIds.length >= 6);
  assert.equal(new Set(sectionIds).size, sectionIds.length);
  assert.equal(new Set(fieldIds).size, fieldIds.length);
  assert.ok(fieldIds.length > 50);
});

test("metadados de grupos referenciam apenas campos existentes na própria seção", () => {
  for (const section of dashboardSections) {
    const fieldIds = new Set(section.fields.map((field) => field.id));
    for (const [group, metadata] of Object.entries(section.groupMetadata ?? {})) {
      assert.ok(section.groups?.includes(group), `${section.id}: metadata para grupo não declarado: ${group}`);
      for (const fieldId of metadata.settingsFieldIds ?? []) {
        assert.ok(fieldIds.has(fieldId), `${section.id}/${group}: settingsFieldId inexistente: ${fieldId}`);
      }
      for (const editor of metadata.editors ?? []) {
        assert.ok(editor.id.trim(), `${section.id}/${group}: editor sem id`);
        for (const fieldId of [...editor.fieldIds, ...(editor.senderFieldIds ?? [])]) {
          assert.ok(fieldIds.has(fieldId), `${section.id}/${group}/${editor.id}: campo inexistente: ${fieldId}`);
        }
      }
    }
  }
});

test("selects não expõem opções duplicadas", () => {
  for (const field of allDashboardFields()) {
    if (!field.options) continue;
    const values = field.options.map((option) => option.value);
    assert.equal(new Set(values).size, values.length, `opção duplicada em ${field.id}`);
  }
});

test("estado semântico exige pré-requisitos antes de marcar função ativa", () => {
  assert.deepEqual(resolveDashboardSectionState("welcome", { "welcome.enabled": true }), {
    enabled: true,
    state: "inactive",
    status: "Desativada",
    issues: ["Selecione o canal de boas-vindas."],
  });
  assert.deepEqual(resolveDashboardSectionState("welcome", {
    "welcome.enabled": true,
    "welcome.channel_id": "123456789012345678",
  }), { enabled: true, state: "active", status: "Ativa", issues: [] });
  assert.equal(isConfiguredValue([]), false);
  assert.equal(isConfiguredValue({ id: 1 }), true);
});
