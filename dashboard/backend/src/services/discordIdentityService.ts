import { getShortCacheValue, setShortCacheValue, tokenCacheKey, type ShortCacheEntry } from "./discordCache.js";
import { discordBotToken, dashboardSupportInviteCode, dashboardSupportInviteUrl } from "./discordAuthConfig.js";
import { fetchDiscordJson, fetchDiscordPublicJson } from "./discordHttpClient.js";
import { guildIconUrl, withAvatarUrl } from "./discordPresentation.js";

export interface DiscordUserIdentity {
  id: string;
  username?: string | null;
  global_name?: string | null;
  avatar?: string | null;
  avatarUrl?: string | null;
}

export interface DiscordSupportServerIdentity {
  id: string;
  name: string;
  icon: string | null;
  inviteUrl: string;
}

const USER_IDENTITY_CACHE_MS = 15_000;
const MAX_SHORT_CACHE_ENTRIES = 2_000;
const userIdentityCache = new Map<string, ShortCacheEntry<DiscordUserIdentity>>();
let botIdentityCache: ShortCacheEntry<DiscordUserIdentity | null> | null = null;
let supportServerCache: ShortCacheEntry<DiscordSupportServerIdentity | null> | null = null;

export async function getDiscordBotIdentity(): Promise<DiscordUserIdentity | null> {
  const token = discordBotToken();
  if (!token) return null;

  const now = Date.now();
  if (botIdentityCache && botIdentityCache.expiresAt > now) return botIdentityCache.value;

  const response = await fetchDiscordJson<DiscordUserIdentity>(
    "https://discord.com/api/v10/users/@me",
    `Bot ${token}`,
  );
  const value = response.ok && response.data && /^\d{15,25}$/.test(String(response.data.id ?? ""))
    ? withAvatarUrl(response.data)
    : null;

  botIdentityCache = { expiresAt: now + 10 * 60 * 1000, value };
  return value;
}

export async function getDiscordSupportServerIdentity(): Promise<DiscordSupportServerIdentity | null> {
  const now = Date.now();
  if (supportServerCache && supportServerCache.expiresAt > now) return supportServerCache.value;

  const code = dashboardSupportInviteCode();
  const response = await fetchDiscordPublicJson<{ guild?: Record<string, unknown> }>(
    `https://discord.com/api/v10/invites/${encodeURIComponent(code)}?with_counts=true&with_expiration=true`,
  );
  const guild = response.ok && response.data?.guild && typeof response.data.guild === "object"
    ? response.data.guild
    : null;
  const id = guild ? String(guild.id ?? "") : "";
  const value = guild && /^\d{15,25}$/.test(id)
    ? {
        id,
        name: String(guild.name ?? "Servidor de suporte"),
        icon: guildIconUrl(id, guild.icon),
        inviteUrl: dashboardSupportInviteUrl(),
      }
    : null;

  supportServerCache = { expiresAt: now + 30 * 60 * 1000, value };
  return value;
}

export async function getDiscordUserIdentity(accessToken: string): Promise<{ ok: boolean; status: number; user: DiscordUserIdentity | null }> {
  if (!accessToken) return { ok: false, status: 401, user: null };
  const now = Date.now();
  const cacheKey = tokenCacheKey(accessToken);
  const cached = getShortCacheValue(userIdentityCache, cacheKey, now);
  if (cached) return { ok: true, status: 200, user: cached };

  const me = await fetchDiscordJson<DiscordUserIdentity>("https://discord.com/api/v10/users/@me", `Bearer ${accessToken}`);
  if (!me.ok || !me.data || !/^\d{15,25}$/.test(String(me.data.id ?? ""))) {
    userIdentityCache.delete(cacheKey);
    return { ok: false, status: me.status || 401, user: null };
  }

  const user = withAvatarUrl(me.data)!;
  setShortCacheValue(userIdentityCache, cacheKey, user, USER_IDENTITY_CACHE_MS, { now, maxEntries: MAX_SHORT_CACHE_ENTRIES });
  return { ok: true, status: 200, user };
}
