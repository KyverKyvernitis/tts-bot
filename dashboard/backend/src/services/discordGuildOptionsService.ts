import { discordBotToken } from "./discordAuthConfig.js";
import { getDiscordBotIdentity } from "./discordIdentityService.js";
import { fetchDiscordJson } from "./discordHttpClient.js";
import { buildDashboardGuildOptions, type DashboardGuildOptionsResult } from "./discordDashboardModel.js";

export async function listGuildChannelsAndRoles(guildId: string): Promise<DashboardGuildOptionsResult> {
  const token = discordBotToken();
  if (!token) return { ok: false, channels: [], roles: [], error: "bot_token_missing" };

  const auth = `Bot ${token}`;
  const botIdentity = await getDiscordBotIdentity();
  const [channelsResp, rolesResp, botMemberResp] = await Promise.all([
    fetchDiscordJson<Array<Record<string, unknown>>>(`https://discord.com/api/v10/guilds/${guildId}/channels`, auth),
    fetchDiscordJson<Array<Record<string, unknown>>>(`https://discord.com/api/v10/guilds/${guildId}/roles`, auth),
    botIdentity?.id
      ? fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}/members/${botIdentity.id}`, auth)
      : Promise.resolve({ ok: false, status: 0, data: null }),
  ]);

  if (!channelsResp.ok || !Array.isArray(channelsResp.data)) {
    return { ok: false, channels: [], roles: [], error: `channels_fetch_failed_${channelsResp.status}` };
  }
  if (!rolesResp.ok || !Array.isArray(rolesResp.data)) {
    return { ok: false, channels: [], roles: [], error: `roles_fetch_failed_${rolesResp.status}` };
  }

  const botRoleIds: Set<string> | null = botMemberResp.ok && botMemberResp.data && Array.isArray(botMemberResp.data.roles)
    ? new Set<string>(botMemberResp.data.roles.map((roleId) => String(roleId)))
    : null;
  const botId = botIdentity?.id && /^\d{15,25}$/.test(botIdentity.id) ? botIdentity.id : null;
  const { channels, roles } = buildDashboardGuildOptions(guildId, botId, botRoleIds, channelsResp.data, rolesResp.data);
  return { ok: true, channels, roles };
}
