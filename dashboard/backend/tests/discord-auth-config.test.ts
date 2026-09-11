import assert from "node:assert/strict";
import test from "node:test";
import {
  createDashboardInviteUrl,
  dashboardAllowedOwners,
  dashboardClientId,
  dashboardSupportInviteCode,
  dashboardSupportInviteUrl,
  discordBotToken,
} from "../src/services/discordAuthConfig.js";

const ENV_KEYS = [
  "DISCORD_BOT_TOKEN", "DISCORD_TOKEN", "BOT_TOKEN", "TOKEN",
  "VITE_DISCORD_CLIENT_ID", "DISCORD_CLIENT_ID", "CLIENT_ID",
  "DASHBOARD_ADMIN_USER_IDS", "OWNER_IDS", "BOT_OWNER_IDS",
  "DASHBOARD_SUPPORT_INVITE_URL", "DASHBOARD_SUPPORT_INVITE_CODE",
  "DASHBOARD_BOT_INVITE_PERMISSIONS", "BOT_INVITE_PERMISSIONS",
] as const;

function withEnv(values: Partial<Record<(typeof ENV_KEYS)[number], string | undefined>>, run: () => void) {
  const previous = new Map<string, string | undefined>();
  for (const key of ENV_KEYS) {
    previous.set(key, process.env[key]);
    delete process.env[key];
  }
  try {
    for (const [key, value] of Object.entries(values)) {
      if (value !== undefined) process.env[key] = value;
    }
    run();
  } finally {
    for (const key of ENV_KEYS) {
      const value = previous.get(key);
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("preserva a precedência de tokens e client ids legados", () => {
  withEnv({ DISCORD_BOT_TOKEN: " primary ", DISCORD_TOKEN: "secondary", VITE_DISCORD_CLIENT_ID: " 123 ", DISCORD_CLIENT_ID: "456" }, () => {
    assert.equal(discordBotToken(), "primary");
    assert.equal(dashboardClientId(), "123");
  });
  withEnv({ BOT_TOKEN: "bot", CLIENT_ID: "client" }, () => {
    assert.equal(discordBotToken(), "bot");
    assert.equal(dashboardClientId(), "client");
  });
});

test("normaliza owners separados por vírgula, ponto e vírgula e espaços", () => {
  withEnv({ DASHBOARD_ADMIN_USER_IDS: " 1,2; 3\n4  " }, () => {
    assert.deepEqual([...dashboardAllowedOwners()], ["1", "2", "3", "4"]);
  });
});

test("extrai código do convite configurado e mantém fallback seguro", () => {
  withEnv({ DASHBOARD_SUPPORT_INVITE_URL: "https://discord.gg/AbCd" }, () => {
    assert.equal(dashboardSupportInviteUrl(), "https://discord.gg/AbCd");
    assert.equal(dashboardSupportInviteCode(), "AbCd");
  });
  withEnv({ DASHBOARD_SUPPORT_INVITE_URL: "://inválido" }, () => {
    assert.equal(dashboardSupportInviteCode(), "RckuzJbvVk");
  });
  withEnv({ DASHBOARD_SUPPORT_INVITE_CODE: " explicit " }, () => {
    assert.equal(dashboardSupportInviteCode(), "explicit");
  });
});

test("gera URL OAuth apenas com client id e só fixa guild ids válidos", () => {
  withEnv({}, () => assert.equal(createDashboardInviteUrl("111111111111111"), null));
  withEnv({ DISCORD_CLIENT_ID: "999", DASHBOARD_BOT_INVITE_PERMISSIONS: "16" }, () => {
    const valid = new URL(createDashboardInviteUrl("111111111111111")!);
    assert.equal(valid.origin + valid.pathname, "https://discord.com/oauth2/authorize");
    assert.equal(valid.searchParams.get("client_id"), "999");
    assert.equal(valid.searchParams.get("permissions"), "16");
    assert.equal(valid.searchParams.get("scope"), "bot applications.commands");
    assert.equal(valid.searchParams.get("guild_id"), "111111111111111");
    assert.equal(valid.searchParams.get("disable_guild_select"), "true");

    const invalid = new URL(createDashboardInviteUrl("abc")!);
    assert.equal(invalid.searchParams.has("guild_id"), false);
    assert.equal(invalid.searchParams.has("disable_guild_select"), false);
  });
});
