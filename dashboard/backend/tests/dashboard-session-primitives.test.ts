import assert from "node:assert/strict";
import test from "node:test";
import {
  DASHBOARD_OAUTH_COOKIE,
  DASHBOARD_SESSION_COOKIE,
  createDashboardSessionId,
  dashboardCookieValue,
  dashboardSessionHash,
  decryptDashboardSessionText,
  deriveDashboardSessionKey,
  encryptDashboardSessionText,
  issueDashboardOAuthState,
  safeDashboardReturnPath,
  serializeDashboardCookie,
  validateDashboardOAuthState,
} from "../src/services/dashboardSessionPrimitives.js";

const SECRET = "dashboard-session-secret-with-at-least-24-chars";

test("return path aceita somente destinos locais esperados", () => {
  assert.equal(safeDashboardReturnPath("/dashboard"), "/dashboard");
  assert.equal(safeDashboardReturnPath(" /dashboard/123?tab=x#top "), "/dashboard/123?tab=x#top");
  assert.equal(safeDashboardReturnPath("/"), "/");
  for (const unsafe of [undefined, "", "https://evil.example/dashboard", "//evil.example/x", "/other", "/dashboard\\evil"]) {
    assert.equal(safeDashboardReturnPath(unsafe), "/dashboard");
  }
});

test("cookies preservam encoding, atributos e não explodem com encoding inválido", () => {
  const cookie = serializeDashboardCookie(DASHBOARD_SESSION_COOKIE, "a b=c", {
    maxAgeSeconds: 12.9,
    secure: true,
    sameSite: "Strict",
  });
  assert.equal(cookie, "osk_dashboard_session=a%20b%3Dc; Path=/; Max-Age=12; HttpOnly; SameSite=Strict; Secure");
  assert.equal(dashboardCookieValue(`x=1; ${cookie.split(";", 1)[0]}; y=2`, DASHBOARD_SESSION_COOKIE), "a b=c");
  assert.equal(dashboardCookieValue(`${DASHBOARD_SESSION_COOKIE}=%E0%A4%A`, DASHBOARD_SESSION_COOKIE), "");
  assert.equal(serializeDashboardCookie("x", "", { maxAgeSeconds: -20, httpOnly: false }), "x=; Path=/; Max-Age=0; SameSite=Lax");
});

test("ids e hashes de sessão usam formato estável", () => {
  const id = createDashboardSessionId();
  assert.match(id, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(dashboardSessionHash("session-id"), dashboardSessionHash("session-id"));
  assert.match(dashboardSessionHash("session-id"), /^[a-f0-9]{64}$/);
});

test("tokens criptografados fazem round-trip e detectam adulteração", () => {
  const key = deriveDashboardSessionKey(SECRET);
  const encrypted = encryptDashboardSessionText("discord-token", key);
  assert.equal(encrypted.split(".").length, 3);
  assert.equal(decryptDashboardSessionText(encrypted, key), "discord-token");

  const parts = encrypted.split(".");
  parts[2] = `${parts[2].startsWith("A") ? "B" : "A"}${parts[2].slice(1)}`;
  assert.throws(() => decryptDashboardSessionText(parts.join("."), key));
  assert.throws(() => decryptDashboardSessionText("broken", key), /invalid_encrypted_session_value/);
});

test("oauth state valida cookie, assinatura, retorno seguro e expiração", () => {
  const issuedAt = 1_700_000_000_000;
  const issued = issueDashboardOAuthState(SECRET, "/dashboard/123?view=modules", true, issuedAt);
  assert.match(issued.setCookie, new RegExp(`^${DASHBOARD_OAUTH_COOKIE}=`));
  assert.match(issued.setCookie, /HttpOnly/);
  assert.match(issued.setCookie, /SameSite=Lax/);
  assert.match(issued.setCookie, /Secure/);

  const cookieHeader = `${DASHBOARD_OAUTH_COOKIE}=${encodeURIComponent(issued.state)}`;
  assert.deepEqual(validateDashboardOAuthState(SECRET, issued.state, cookieHeader, issuedAt + 1), {
    ok: true,
    returnTo: "/dashboard/123?view=modules",
  });
  assert.deepEqual(validateDashboardOAuthState(SECRET, issued.state, cookieHeader, issuedAt + 10 * 60 * 1000 + 1), {
    ok: false,
    reason: "oauth_state_expired",
  });

  const [payload, signature] = issued.state.split(".");
  const tamperedSignature = `${signature.startsWith("A") ? "B" : "A"}${signature.slice(1)}`;
  const tampered = `${payload}.${tamperedSignature}`;
  assert.deepEqual(
    validateDashboardOAuthState(SECRET, tampered, `${DASHBOARD_OAUTH_COOKIE}=${encodeURIComponent(tampered)}`, issuedAt + 1),
    { ok: false, reason: "oauth_state_invalid" },
  );
  assert.deepEqual(validateDashboardOAuthState(SECRET, issued.state, undefined, issuedAt + 1), {
    ok: false,
    reason: "oauth_state_mismatch",
  });
});
