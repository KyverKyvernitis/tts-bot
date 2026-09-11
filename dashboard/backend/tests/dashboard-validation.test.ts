import assert from "node:assert/strict";
import test from "node:test";
import { DashboardConfigValueError } from "../src/config/dashboardTypes.js";
import {
  isDashboardTimeZone,
  normalizeDashboardPrefix,
  normalizeDashboardTimeZone,
  validateDashboardPrefixes,
} from "../src/config/dashboardValidation.js";

test("aceita fusos IANA válidos e rejeita entradas inventadas", () => {
  assert.equal(isDashboardTimeZone("America/Sao_Paulo"), true);
  assert.equal(isDashboardTimeZone("UTC"), true);
  assert.equal(isDashboardTimeZone("America/Osaka_Impossivel"), false);
  assert.equal(isDashboardTimeZone("x".repeat(65)), false);
  assert.equal(normalizeDashboardTimeZone(" America/Sao_Paulo "), "America/Sao_Paulo");
  assert.throws(
    () => normalizeDashboardTimeZone("timezone-invalido"),
    (error: unknown) => error instanceof DashboardConfigValueError && error.fieldId === "general.timezone",
  );
});

test("normaliza tamanho do prefixo e bloqueia colisões", () => {
  assert.equal(normalizeDashboardPrefix("  abcdefghijk  "), "abcdefgh");
  const valid = {
    bot_prefix: "_",
    atts_prefix: "%",
    teto_prefix: "'",
    gtts_prefix: ".",
    edge_prefix: ",",
  };
  assert.doesNotThrow(() => validateDashboardPrefixes(valid));
  assert.throws(() => validateDashboardPrefixes({ ...valid, edge_prefix: "_" }), /não podem ser iguais/);
});

test("prefixos não podem ficar vazios nem conter espaço ou controle", () => {
  const base = {
    bot_prefix: "_",
    atts_prefix: "%",
    teto_prefix: "'",
    gtts_prefix: ".",
    edge_prefix: ",",
  };
  assert.throws(() => validateDashboardPrefixes({ ...base, bot_prefix: " " }), /não pode ficar vazio/);
  assert.throws(() => validateDashboardPrefixes({ ...base, bot_prefix: "a b" }), /não pode conter espaços/);
  assert.throws(() => validateDashboardPrefixes({ ...base, bot_prefix: "\u0001" }), /caracteres invisíveis/);
});
