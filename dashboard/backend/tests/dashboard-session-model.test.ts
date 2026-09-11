import assert from "node:assert/strict";
import test from "node:test";
import {
  DASHBOARD_REFRESH_MARGIN_MS,
  dashboardAccessExpiresAt,
  dashboardNextRefreshToken,
  dashboardSessionExpired,
  dashboardSessionNeedsRefresh,
} from "../src/services/dashboardSessionModel.js";

test("expiração do access token aceita somente duração positiva e finita", () => {
  const now = 1_700_000_000_000;
  assert.equal(dashboardAccessExpiresAt(60, now), now + 60_000);
  assert.equal(dashboardAccessExpiresAt(0, now), null);
  assert.equal(dashboardAccessExpiresAt(-1, now), null);
  assert.equal(dashboardAccessExpiresAt(Number.POSITIVE_INFINITY, now), null);
  assert.equal(dashboardAccessExpiresAt(null, now), null);
});

test("sessão expirada trata timestamps inválidos de forma conservadora", () => {
  const now = 1_000;
  assert.equal(dashboardSessionExpired(new Date(999), now), true);
  assert.equal(dashboardSessionExpired(new Date(1001), now), false);
  assert.equal(dashboardSessionExpired(Number.NaN, now), true);
});

test("refresh acontece dentro da margem e não para token sem validade conhecida", () => {
  const now = 10_000;
  assert.equal(dashboardSessionNeedsRefresh(null, now), false);
  assert.equal(dashboardSessionNeedsRefresh(now + DASHBOARD_REFRESH_MARGIN_MS - 1, now), true);
  assert.equal(dashboardSessionNeedsRefresh(now + DASHBOARD_REFRESH_MARGIN_MS + 1, now), false);
  assert.equal(dashboardSessionNeedsRefresh(now + 1, now, -50), false);
});

test("refresh token preserva exatamente o contrato de substituição nulo versus presente", () => {
  assert.equal(dashboardNextRefreshToken("old", "new"), "new");
  assert.equal(dashboardNextRefreshToken("old", ""), "");
  assert.equal(dashboardNextRefreshToken("old", null), "old");
});
