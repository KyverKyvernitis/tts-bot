import assert from "node:assert/strict";
import test from "node:test";
import type { Request } from "express";
import {
  authErrorRedirect,
  dashboardGuildId,
  firstString,
  isSecureRequest,
  mutationOriginAllowed,
  requestOrigin,
  takeRateLimit,
} from "../src/routes/dashboardRouteUtils";

function request(overrides: Partial<Request> = {}): Request {
  return {
    params: {},
    query: {},
    body: {},
    headers: {},
    secure: false,
    ip: "127.0.0.1",
    socket: { remoteAddress: "127.0.0.1" },
    ...overrides,
  } as Request;
}

test("firstString ignora vazios mas preserva zero numérico", () => {
  assert.equal(firstString(undefined, " ", 0, "x"), "0");
  assert.equal(firstString(null, " guild "), "guild");
});

test("guildId prioriza parâmetro de rota antes de query e body", () => {
  const req = request({
    params: { guildId: "111" },
    query: { guild_id: "222" },
    body: { guild_id: "333", guildId: "444" },
  } as Partial<Request>);
  assert.equal(dashboardGuildId(req), "111");
});

test("reconhece HTTPS direto e atrás de proxy", () => {
  assert.equal(isSecureRequest(request({ secure: true })), true);
  assert.equal(isSecureRequest(request({ headers: { "x-forwarded-proto": "https,http" } })), true);
  assert.equal(isSecureRequest(request({ headers: { "x-forwarded-proto": "http" } })), false);
});

test("origem configurada prevalece e produção não infere host inseguro", () => {
  const req = request({ headers: { host: "localhost:5173" } });
  assert.equal(requestOrigin(req, "https://dashboard.example"), "https://dashboard.example");
  const old = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  try {
    assert.equal(requestOrigin(req, ""), "");
  } finally {
    if (old === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = old;
  }
});

test("validação de Origin aceita origem canônica/allowlist e rejeita inválida", () => {
  const base = request({ headers: { origin: "https://dashboard.example" } });
  assert.equal(mutationOriginAllowed(base, "https://dashboard.example", new Set()), true);
  assert.equal(mutationOriginAllowed(request({ headers: { origin: "https://admin.example" } }), "https://dashboard.example", new Set(["https://admin.example"])), true);
  assert.equal(mutationOriginAllowed(request({ headers: { origin: "https://evil.example" } }), "https://dashboard.example", new Set()), false);
  assert.equal(mutationOriginAllowed(request({ headers: { origin: "::::" } }), "https://dashboard.example", new Set()), false);
});

test("rate limit separa buckets e bloqueia após o limite", () => {
  const req = request({ ip: `test-${Date.now()}-${Math.random()}` });
  assert.equal(takeRateLimit(req, "a", 2, 60_000), true);
  assert.equal(takeRateLimit(req, "a", 2, 60_000), true);
  assert.equal(takeRateLimit(req, "a", 2, 60_000), false);
  assert.equal(takeRateLimit(req, "b", 1, 60_000), true);
});

test("redirecionamento de erro OAuth mantém apenas código interno", () => {
  const req = request({ headers: { host: "dashboard.example" }, secure: true });
  assert.equal(authErrorRedirect(req, "https://dashboard.example", "state_invalid"), "/?auth_error=state_invalid");
});
