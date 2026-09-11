import assert from "node:assert/strict";
import test from "node:test";
import {
  PERMISSION_ADMINISTRATOR,
  PERMISSION_CONNECT,
  PERMISSION_MANAGE_CHANNELS,
  PERMISSION_MANAGE_GUILD,
  PERMISSION_MANAGE_WEBHOOKS,
  PERMISSION_SEND_MESSAGES,
  PERMISSION_VIEW_CHANNEL,
  botBasePermissions,
  botChannelPermissions,
  channelCapabilities,
  permissionBits,
  permissionFromRoles,
  userCanManageGuild,
} from "../src/services/discordPermissions";
import { discordAvatarUrl, guildIconUrl, mapWithConcurrency } from "../src/services/discordPresentation";

test("converte permissões inválidas para zero sem lançar", () => {
  assert.equal(permissionBits("123"), 123n);
  assert.equal(permissionBits("not-a-number"), 0n);
  assert.equal(permissionBits(null), 0n);
});

test("calcula permissões base do bot incluindo everyone e cargos atribuídos", () => {
  const roles = [
    { id: "guild", permissions: String(PERMISSION_VIEW_CHANNEL) },
    { id: "bot-role", permissions: String(PERMISSION_SEND_MESSAGES | PERMISSION_CONNECT) },
    { id: "other", permissions: String(PERMISSION_ADMINISTRATOR) },
  ];
  assert.equal(
    botBasePermissions("guild", new Set(["bot-role"]), roles),
    PERMISSION_VIEW_CHANNEL | PERMISSION_SEND_MESSAGES | PERMISSION_CONNECT,
  );
});

test("aplica overwrites Discord na ordem everyone, cargos e membro", () => {
  const base = PERMISSION_VIEW_CHANNEL | PERMISSION_SEND_MESSAGES | PERMISSION_CONNECT;
  const channel = {
    permission_overwrites: [
      { id: "guild", type: 0, deny: String(PERMISSION_SEND_MESSAGES), allow: "0" },
      { id: "bot-role", type: 0, deny: String(PERMISSION_CONNECT), allow: String(PERMISSION_SEND_MESSAGES) },
      { id: "bot", type: 1, deny: "0", allow: String(PERMISSION_CONNECT) },
    ],
  };
  const permissions = botChannelPermissions(channel, "guild", "bot", new Set(["bot-role"]), base);
  assert.equal((permissions & PERMISSION_VIEW_CHANNEL) !== 0n, true);
  assert.equal((permissions & PERMISSION_SEND_MESSAGES) !== 0n, true);
  assert.equal((permissions & PERMISSION_CONNECT) !== 0n, true);
});

test("administrador ignora overwrites e mantém todas as capacidades", () => {
  const permissions = botChannelPermissions({
    permission_overwrites: [{ id: "guild", type: 0, deny: String(PERMISSION_VIEW_CHANNEL), allow: "0" }],
  }, "guild", "bot", new Set(), PERMISSION_ADMINISTRATOR);
  assert.equal(permissions, PERMISSION_ADMINISTRATOR);
  assert.deepEqual(channelCapabilities(permissions), {
    permissionsKnown: true,
    viewable: true,
    sendable: true,
    connectable: true,
    manageable: true,
    webhookManageable: true,
  });
});

test("capacidades desconhecidas preservam compatibilidade em vez de bloquear opções", () => {
  assert.deepEqual(channelCapabilities(null), {
    permissionsKnown: false,
    viewable: true,
    sendable: true,
    connectable: true,
    manageable: true,
    webhookManageable: true,
  });
  const caps = channelCapabilities(PERMISSION_VIEW_CHANNEL | PERMISSION_MANAGE_CHANNELS | PERMISSION_MANAGE_WEBHOOKS);
  assert.equal(caps.viewable, true);
  assert.equal(caps.sendable, false);
  assert.equal(caps.connectable, false);
  assert.equal(caps.manageable, true);
  assert.equal(caps.webhookManageable, true);
});

test("permissões do usuário incluem everyone e reconhecem owner/manage guild", () => {
  const roles = [
    { id: "guild", name: "@everyone", permissions: String(PERMISSION_VIEW_CHANNEL) },
    { id: "manager", name: "Manager", permissions: String(PERMISSION_MANAGE_GUILD) },
  ];
  assert.equal(
    permissionFromRoles(["manager"], roles, null, "user", "guild"),
    PERMISSION_VIEW_CHANNEL | PERMISSION_MANAGE_GUILD,
  );
  assert.equal(userCanManageGuild([{ id: "guild", permissions: String(PERMISSION_MANAGE_GUILD), owner: false }], "guild"), true);
  assert.equal(userCanManageGuild([{ id: "guild", permissions: "0", owner: true }], "guild"), true);
  assert.equal(userCanManageGuild([{ id: "guild", permissions: "0", owner: false }], "guild"), false);
});

test("gera URLs CDN animadas e processa concorrência preservando ordem", async () => {
  assert.equal(guildIconUrl("1", "hash"), "https://cdn.discordapp.com/icons/1/hash.png?size=128");
  assert.equal(guildIconUrl("1", "a_hash"), "https://cdn.discordapp.com/icons/1/a_hash.gif?size=128");
  assert.equal(discordAvatarUrl("2", "a_avatar"), "https://cdn.discordapp.com/avatars/2/a_avatar.gif?size=128");
  assert.equal(discordAvatarUrl("", "avatar"), null);

  let active = 0;
  let maxActive = 0;
  const values = await mapWithConcurrency([1, 2, 3, 4], 2, async (value) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((resolve) => setTimeout(resolve, value % 2 ? 3 : 1));
    active -= 1;
    return value * 10;
  });
  assert.deepEqual(values, [10, 20, 30, 40]);
  assert.ok(maxActive <= 2);
});
