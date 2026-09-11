import { createDashboardInviteUrl, discordBotToken } from "./discordAuthConfig.js";
import { getDiscordUserIdentity, type DiscordUserIdentity } from "./discordIdentityService.js";
import { fetchDiscordJson } from "./discordHttpClient.js";
import { mapWithConcurrency } from "./discordPresentation.js";
import {
  eligibleDashboardGuilds,
  partitionDashboardServerCards,
  type DashboardServerCard,
} from "./discordDashboardModel.js";

export interface DashboardServerListResult {
  ok: boolean;
  status: number;
  user: DiscordUserIdentity | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  error?: string | null;
}

async function fetchBotGuildIds(): Promise<Set<string> | null> {
  const token = discordBotToken();
  if (!token) return null;
  const response = await fetchDiscordJson<Array<Record<string, unknown>>>("https://discord.com/api/v10/users/@me/guilds", `Bot ${token}`);
  if (!response.ok || !Array.isArray(response.data)) return null;
  return new Set(response.data.map((guild) => String(guild.id ?? "")).filter((id) => /^\d{15,25}$/.test(id)));
}

async function checkBotInGuild(guildId: string, botGuildIds: Set<string> | null): Promise<boolean> {
  if (botGuildIds) return botGuildIds.has(guildId);
  const token = discordBotToken();
  if (!token) return false;
  const response = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}`, `Bot ${token}`);
  return response.ok;
}

export async function listDashboardServers(accessToken: string, knownUser?: DiscordUserIdentity | null): Promise<DashboardServerListResult> {
  const userResult = knownUser
    ? { ok: true, status: 200, user: knownUser }
    : await getDiscordUserIdentity(accessToken);
  if (!userResult.ok || !userResult.user) {
    return { ok: false, status: userResult.status || 401, user: null, manageable: [], needsInvite: [], error: "user_fetch_failed" };
  }

  const guildsResp = await fetchDiscordJson<Array<Record<string, unknown>>>("https://discord.com/api/v10/users/@me/guilds", `Bearer ${accessToken}`);
  if (!guildsResp.ok || !Array.isArray(guildsResp.data)) {
    return { ok: false, status: guildsResp.status || 400, user: userResult.user, manageable: [], needsInvite: [], error: "guilds_fetch_failed" };
  }

  const botGuildIds = await fetchBotGuildIds();
  const eligibleGuilds = eligibleDashboardGuilds(guildsResp.data);
  const botPresence = botGuildIds
    ? eligibleGuilds.map((guild) => botGuildIds.has(String(guild.id ?? "")))
    : await mapWithConcurrency(eligibleGuilds, 4, async (guild) => checkBotInGuild(String(guild.id ?? ""), null));
  const { manageable, needsInvite } = partitionDashboardServerCards(eligibleGuilds, botPresence, createDashboardInviteUrl);
  return { ok: true, status: 200, user: userResult.user, manageable, needsInvite };
}
