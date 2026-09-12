import assert from "node:assert/strict";
import test from "node:test";
import { normalizeInputValue, valuesEqual } from "../src/app/dashboardValues";
import { errorText } from "../src/app/errors";
import { DashboardHttpError } from "../src/transport/httpClient";
import type { DashboardFieldDefinition } from "../src/types/dashboard";

function field(type: DashboardFieldDefinition["type"], id = `x.${type}`): DashboardFieldDefinition {
  return { id, label: id, type, scope: "guild", path: id };
}

test("normaliza valores simples sem alterar estruturas complexas", () => {
  assert.equal(normalizeInputValue(field("boolean"), 1), true);
  assert.equal(normalizeInputValue(field("number"), "12.5"), 12.5);
  assert.equal(normalizeInputValue(field("number"), "invalido"), 0);
  assert.equal(normalizeInputValue(field("channel"), "<#123456789012345678>"), "123456789012345678");
  assert.equal(normalizeInputValue(field("role"), "sem-id"), "");
  const complex = [{ id: "a" }];
  assert.equal(normalizeInputValue(field("form_fields"), complex), complex);
});

test("compara drafts serializáveis e falha de forma segura para ciclos", () => {
  assert.equal(valuesEqual({ a: [1, 2] }, { a: [1, 2] }), true);
  assert.equal(valuesEqual({ a: 1 }, { a: 2 }), false);
  const a: Record<string, unknown> = {}; a.self = a;
  const b: Record<string, unknown> = {}; b.self = b;
  assert.equal(valuesEqual(a, b), false);
});

test("traduz erros HTTP conhecidos e preserva mensagens desconhecidas", () => {
  assert.equal(errorText(new DashboardHttpError("x", 401, "http_error", { error: "session_required" })), "Sua sessão expirou. Entre novamente com o Discord.");
  assert.equal(errorText(new DashboardHttpError("backend_custom", 500, "http_error", { error: "custom" })), "backend_custom");
  assert.equal(errorText("x"), "Ocorreu uma falha inesperada.");
});
