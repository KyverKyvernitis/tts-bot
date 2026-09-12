import assert from "node:assert/strict";
import test from "node:test";
import { isSnowflake, parseRoute, routePath, type Route } from "../src/app/routing";

const guildId = "123456789012345678";

const roundTrips: Route[] = [
  { page: "landing" },
  { page: "privacy" },
  { page: "terms" },
  { page: "servers" },
  { page: "invite", guildId },
  { page: "dashboard", guildId, view: "general", moduleId: null },
  { page: "dashboard", guildId, view: "modules", moduleId: null },
  { page: "dashboard", guildId, view: "commands", moduleId: null },
  { page: "dashboard", guildId, view: "module", moduleId: "welcome", },
];

test("faz round-trip das rotas canônicas do dashboard", () => {
  for (const route of roundTrips) assert.deepEqual(parseRoute(routePath(route)), route);
});

test("mantém aliases legais e rotas antigas de módulo compatíveis", () => {
  assert.deepEqual(parseRoute("/privacidade"), { page: "privacy" });
  assert.deepEqual(parseRoute("/termos"), { page: "terms" });
  assert.deepEqual(parseRoute(`/dashboard/${guildId}/general`), { page: "dashboard", guildId, view: "general", moduleId: null });
  assert.deepEqual(parseRoute(`/dashboard/${guildId}/welcome`), { page: "dashboard", guildId, view: "module", moduleId: "welcome" });
});

test("rejeita IDs e segmentos ambíguos sem escapar para rotas privilegiadas", () => {
  assert.equal(isSnowflake(guildId), true);
  assert.equal(isSnowflake("123"), false);
  assert.deepEqual(parseRoute("/dashboard/123/modulos/welcome"), { page: "landing" });
  assert.deepEqual(parseRoute(`/dashboard/${guildId}/modulos/a/b`), { page: "dashboard", guildId, view: "modules", moduleId: null });
  assert.deepEqual(parseRoute(`/dashboard/${guildId}/modulos/%2e%2e`), { page: "dashboard", guildId, view: "modules", moduleId: null });
});
