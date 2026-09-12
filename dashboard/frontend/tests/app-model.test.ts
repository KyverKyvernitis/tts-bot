import assert from "node:assert/strict";
import test from "node:test";
import type { DashboardSectionDefinition } from "../src/types/dashboard";
import {
  changedFieldsForSection,
  isProtectedRoute,
  loginReturnPath,
  saveSuccessText,
  selectedSectionIdForRoute,
} from "../src/app/appModel";

const guildId = "123456789012345678";
const section: DashboardSectionDefinition = {
  id: "general",
  label: "Geral",
  emoji: "⚙️",
  description: "",
  fields: [
    { id: "a", label: "A", type: "text", scope: "guild", path: "a" },
    { id: "b", label: "B", type: "role_multi", scope: "guild", path: "b" },
  ],
};

test("seleciona seção apenas para geral ou módulo explícito", () => {
  assert.equal(selectedSectionIdForRoute({ page: "landing" }), null);
  assert.equal(selectedSectionIdForRoute({ page: "dashboard", guildId, view: "general", moduleId: null }), "general");
  assert.equal(selectedSectionIdForRoute({ page: "dashboard", guildId, view: "modules", moduleId: null }), null);
  assert.equal(selectedSectionIdForRoute({ page: "dashboard", guildId, view: "module", moduleId: "welcome" }), "welcome");
});

test("detecta alterações por valor sem marcar estruturas equivalentes", () => {
  assert.deepEqual(changedFieldsForSection(section, { a: "x", b: ["1", "2"] }, { a: "x", b: ["1", "2"] }), []);
  assert.deepEqual(changedFieldsForSection(section, { a: "x", b: ["1"] }, { a: "y", b: ["1"] }).map((field) => field.id), ["a"]);
  assert.deepEqual(changedFieldsForSection(null, {}, {}), []);
});

test("proteção e retorno de login preservam contratos de navegação", () => {
  assert.equal(isProtectedRoute({ page: "landing" }), false);
  assert.equal(isProtectedRoute({ page: "servers" }), true);
  assert.equal(isProtectedRoute({ page: "invite", guildId }), true);
  assert.equal(loginReturnPath({ page: "privacy" }), "/dashboard");
  assert.equal(loginReturnPath({ page: "dashboard", guildId, view: "commands", moduleId: null }), `/dashboard/${guildId}/comandos`);
});

test("texto de sucesso preserva singular, plural e aviso de resumo", () => {
  assert.equal(saveSuccessText(1, false), "1 alteração salva. O bot sincronizará os módulos compatíveis automaticamente.");
  assert.equal(saveSuccessText(2, false), "2 alterações salvas. O bot sincronizará os módulos compatíveis automaticamente.");
  assert.equal(saveSuccessText(2, true), "2 alterações salvas. O resumo será atualizado na próxima abertura.");
});

// A função de erro do bootstrap fica fora do hook para ser verificável sem React.
