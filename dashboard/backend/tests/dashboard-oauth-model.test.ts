import assert from "node:assert/strict";
import test from "node:test";
import { buildDiscordOAuthAuthorizeUrl } from "../src/routes/dashboardOAuthModel.js";

test("oauth authorize URL preserves Discord contract and encodes values", () => {
  const url = new URL(buildDiscordOAuthAuthorizeUrl("client 123", "https://example.com/dashboard/callback?a=1", "state/+/="));
  assert.equal(url.origin + url.pathname, "https://discord.com/oauth2/authorize");
  assert.equal(url.searchParams.get("client_id"), "client 123");
  assert.equal(url.searchParams.get("redirect_uri"), "https://example.com/dashboard/callback?a=1");
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("scope"), "identify guilds");
  assert.equal(url.searchParams.get("state"), "state/+/=");
  assert.equal(url.searchParams.get("prompt"), "consent");
});
