import assert from "node:assert/strict";
import test from "node:test";
import { createDashboardFullLoader } from "../src/services/dashboardFullLoader.js";

const auth = {
  guildId: "123456789012345678",
  user: { id: "42", username: "core" },
};

function dependencies(overrides: Record<string, unknown> = {}) {
  return {
    configService: {
      async getSettings(guildId: string) {
        return { guildId, sections: [{ id: "general" }], values: { prefix: "!" } } as never;
      },
      async getSummary(guildId: string) {
        return { guildId, sections: [{ id: "general", enabled: true }] } as never;
      },
    },
    async listGuildOptions() {
      return { ok: true, channels: [{ id: "1", name: "geral" }], roles: [{ id: "2", name: "mod" }], error: null } as never;
    },
    async getBotIdentity() {
      return { id: "99", username: "Osaka" };
    },
    ...overrides,
  };
}

test("monta payload completo e reporta todas as etapas", async () => {
  const steps: string[] = [];
  const load = createDashboardFullLoader(dependencies() as never);
  const result = await load(auth, (step) => steps.push(step));
  assert.equal(result.ok, true);
  assert.equal(result.guildId, auth.guildId);
  assert.equal(result.user, auth.user);
  assert.equal(result.values.prefix, "!");
  assert.equal(result.options.guildId, auth.guildId);
  assert.equal(result.options.channels.length, 1);
  assert.deepEqual(new Set(steps), new Set(["settings", "summary", "options", "bot"]));
});

test("falhas auxiliares de opções e bot degradam sem derrubar settings", async () => {
  const load = createDashboardFullLoader(dependencies({
    async listGuildOptions() { throw new Error("discord unavailable"); },
    async getBotIdentity() { throw new Error("discord unavailable"); },
  }) as never);
  const result = await load(auth);
  assert.equal(result.bot, null);
  assert.deepEqual(result.options, {
    ok: false,
    guildId: auth.guildId,
    channels: [],
    roles: [],
    error: "options_failed",
  });
});

test("falha de settings continua sendo fatal para o carregamento completo", async () => {
  const deps = dependencies();
  deps.configService.getSettings = async () => { throw new Error("settings_failed"); };
  const load = createDashboardFullLoader(deps as never);
  await assert.rejects(() => load(auth), /settings_failed/);
});
