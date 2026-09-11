import { dashboardAllowedOwners, discordBotToken } from "./discordAuthConfig.js";
import { getShortCacheValue, setShortCacheValue, type ShortCacheEntry } from "./discordCache.js";
import { getDiscordUserIdentity, type DiscordUserIdentity } from "./discordIdentityService.js";
import { fetchDiscordJson } from "./discordHttpClient.js";
import {
  PERMISSION_ADMINISTRATOR,
  PERMISSION_MANAGE_GUILD,
  permissionFromRoles,
  userCanManageGuild,
} from "./discordPermissions.js";

export interface DashboardAccessResult {
  ok: boolean;
  status: number;
  user: DiscordUserIdentity | null;
  reason: string | null;
  detail?: string | null;
}

const DASHBOARD_ACCESS_CACHE_MS = 15_000;
const MAX_SHORT_CACHE_ENTRIES = 2_000;
const dashboardAccessCache = new Map<string, ShortCacheEntry<{ ok: boolean; reason: string }>>();

async function hasGuildAdminPermission(guildId: string, userId: string): Promise<{ ok: boolean; reason: string }> {
  if (dashboardAllowedOwners().has(userId)) return { ok: true, reason: "owner_env" };

  const now = Date.now();
  const cacheKey = `${guildId}:${userId}`;
  const cached = getShortCacheValue(dashboardAccessCache, cacheKey, now);
  if (cached) return cached;

  const remember = (value: { ok: boolean; reason: string }) => {
    setShortCacheValue(dashboardAccessCache, cacheKey, value, DASHBOARD_ACCESS_CACHE_MS, { now, maxEntries: MAX_SHORT_CACHE_ENTRIES });
    return value;
  };

  const token = discordBotToken();
  if (!token) return { ok: false, reason: "bot_token_missing" };

  const auth = `Bot ${token}`;
  const guildResp = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}`, auth);
  if (!guildResp.ok || !guildResp.data) return { ok: false, reason: `guild_fetch_failed_${guildResp.status}` };

  const ownerId = typeof guildResp.data.owner_id === "string" ? guildResp.data.owner_id : null;
  if (ownerId && ownerId === userId) return remember({ ok: true, reason: "guild_owner" });

  const memberResp = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}/members/${userId}`, auth);
  if (!memberResp.ok || !memberResp.data) return { ok: false, reason: `member_fetch_failed_${memberResp.status}` };

  const rolesResp = await fetchDiscordJson<Array<Record<string, unknown>>>(`https://discord.com/api/v10/guilds/${guildId}/roles`, auth);
  if (!rolesResp.ok || !Array.isArray(rolesResp.data)) return { ok: false, reason: `roles_fetch_failed_${rolesResp.status}` };

  const memberRoles = Array.isArray(memberResp.data.roles) ? memberResp.data.roles.map((item) => String(item)) : [];
  const permissions = permissionFromRoles(memberRoles, rolesResp.data, ownerId, userId, guildId);
  if ((permissions & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR) return remember({ ok: true, reason: "administrator" });
  if ((permissions & PERMISSION_MANAGE_GUILD) === PERMISSION_MANAGE_GUILD) return remember({ ok: true, reason: "manage_guild" });
  return remember({ ok: false, reason: "missing_manage_guild" });
}

export async function verifyDashboardInviteAccess(
  accessToken: string,
  guildId: string,
): Promise<{ ok: boolean; status: number; reason: string }> {
  if (!accessToken) return { ok: false, status: 401, reason: "missing_access_token" };
  if (!/^\d{15,25}$/.test(guildId)) return { ok: false, status: 400, reason: "invalid_guild_id" };

  const guildsResp = await fetchDiscordJson<Array<Record<string, unknown>>>(
    "https://discord.com/api/v10/users/@me/guilds",
    `Bearer ${accessToken}`,
  );
  if (!guildsResp.ok || !Array.isArray(guildsResp.data)) {
    const status = guildsResp.status === 401 || guildsResp.status === 403 ? 401 : 502;
    return { ok: false, status, reason: "guilds_fetch_failed" };
  }
  if (!userCanManageGuild(guildsResp.data, guildId)) {
    return { ok: false, status: 403, reason: "missing_manage_guild" };
  }
  return { ok: true, status: 200, reason: "manageable_guild" };
}

export async function verifyDashboardAccess(accessToken: string, guildId: string, knownUser?: DiscordUserIdentity | null): Promise<DashboardAccessResult> {
  if (!accessToken) return { ok: false, status: 401, user: null, reason: "missing_access_token" };
  if (!/^\d{15,25}$/.test(guildId)) return { ok: false, status: 400, user: null, reason: "invalid_guild_id" };

  const me = knownUser
    ? { ok: true, status: 200, user: knownUser }
    : await getDiscordUserIdentity(accessToken);
  if (!me.ok || !me.user) {
    return { ok: false, status: 401, user: null, reason: `user_fetch_failed_${me.status}` };
  }

  const permission = await hasGuildAdminPermission(guildId, me.user.id);
  if (!permission.ok) {
    return { ok: false, status: 403, user: me.user, reason: permission.reason };
  }

  return { ok: true, status: 200, user: me.user, reason: permission.reason };
}
