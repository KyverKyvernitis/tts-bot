import assert from "node:assert/strict";
import test from "node:test";
import {
  PERMISSION_MANAGE_GUILD,
  PERMISSION_SEND_MESSAGES,
  PERMISSION_VIEW_CHANNEL,
} from "../src/services/discordPermissions";
import {
  buildDashboardGuildOptions,
  eligibleDashboardGuilds,
  partitionDashboardServerCards,
} from "../src/services/discordDashboardModel";

const guildId = "111111111111111";
const botId = "222222222222222";
const botRoleId = "333333333333333";

test("mapeia canais com permissões e cargos atribuíveis pela hierarquia do bot", () => {
  const roles = [
    { id: guildId, name: "@everyone", permissions: String(PERMISSION_VIEW_CHANNEL), position: 0 },
    { id: botRoleId, name: "Osaka", permissions: String(PERMISSION_SEND_MESSAGES), position: 10 },
    { id: "444444444444444", name: "Baixo", permissions: "0", position: 5, managed: false, color: 0x112233 },
    { id: "555555555555555", name: "Alto", permissions: "0", position: 20, managed: false },
    { id: "666666666666666", name: "Integração", permissions: "0", position: 1, managed: true },
  ];
  const channels = [{
    id: "777777777777777",
    name: "geral",
    type: 0,
    permission_overwrites: [],
  }];
  const result = buildDashboardGuildOptions(guildId, botId, new Set([botRoleId]), channels, roles);
  assert.deepEqual(result.channels[0], {
    id: "777777777777777",
    name: "geral",
    type: 0,
    parentId: null,
    permissionsKnown: true,
    viewable: true,
    sendable: true,
    connectable: false,
    manageable: false,
    webhookManageable: false,
  });
  assert.deepEqual(result.roles.map((role) => [role.name, role.assignable]), [["Osaka", false], ["Baixo", true], ["Alto", false], ["Integração", false]]);
});

test("mantém opções de canal compatíveis quando identidade/permissões do bot são desconhecidas", () => {
  const result = buildDashboardGuildOptions(guildId, null, null, [{
    id: "777777777777777", name: "geral", type: 0,
  }], []);
  assert.equal(result.channels[0].permissionsKnown, false);
  assert.equal(result.channels[0].sendable, true);
  assert.equal(result.channels[0].connectable, true);
});

test("filtra apenas guilds válidas que o usuário pode administrar", () => {
  const guilds = [
    { id: guildId, name: "A", permissions: String(PERMISSION_MANAGE_GUILD), owner: false },
    { id: "222222222222223", name: "B", permissions: "0", owner: true },
    { id: "333333333333334", name: "C", permissions: "0", owner: false },
    { id: "x", name: "Inválido", permissions: String(PERMISSION_MANAGE_GUILD), owner: true },
  ];
  assert.deepEqual(eligibleDashboardGuilds(guilds).map((guild) => guild.name), ["A", "B"]);
});

test("particiona presença do bot, cria convite só quando necessário e ordena cartões", () => {
  const guilds = [
    { id: "111111111111112", name: "Zulu", permissions: String(PERMISSION_MANAGE_GUILD), owner: false, icon: null },
    { id: "111111111111113", name: "Alpha", permissions: "0", owner: true, icon: "a_hash" },
  ];
  const result = partitionDashboardServerCards(guilds, [true, false], (id) => `invite:${id}`);
  assert.equal(result.manageable.length, 1);
  assert.equal(result.manageable[0].name, "Zulu");
  assert.equal(result.manageable[0].inviteUrl, null);
  assert.equal(result.needsInvite.length, 1);
  assert.equal(result.needsInvite[0].name, "Alpha");
  assert.equal(result.needsInvite[0].inviteUrl, "invite:111111111111113");
  assert.match(result.needsInvite[0].icon ?? "", /\.gif\?size=128$/);
});
