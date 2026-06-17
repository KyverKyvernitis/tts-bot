export interface DiscordUserIdentity {
  id: string;
  username?: string | null;
  global_name?: string | null;
  avatar?: string | null;
}

export interface DashboardAccessResult {
  ok: boolean;
  status: number;
  user: DiscordUserIdentity | null;
  reason: string | null;
  detail?: string | null;
}

export interface DashboardServerCard {
  id: string;
  name: string;
  icon: string | null;
  owner: boolean;
  permissions: string;
  botPresent: boolean;
  canManage: boolean;
  canInvite: boolean;
  reason: string;
  inviteUrl?: string | null;
}

export interface DashboardServerListResult {
  ok: boolean;
  status: number;
  user: DiscordUserIdentity | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  error?: string | null;
}

const PERMISSION_ADMINISTRATOR = 0x0000000000000008n;
const PERMISSION_MANAGE_GUILD = 0x0000000000000020n;

function botToken(): string {
  return String(process.env.DISCORD_BOT_TOKEN || process.env.DISCORD_TOKEN || process.env.BOT_TOKEN || process.env.TOKEN || "").trim();
}

function clientId(): string {
  return String(process.env.VITE_DISCORD_CLIENT_ID || process.env.DISCORD_CLIENT_ID || process.env.CLIENT_ID || "").trim();
}

function parseAllowedOwners(): Set<string> {
  const raw = String(process.env.DASHBOARD_ADMIN_USER_IDS || process.env.OWNER_IDS || process.env.BOT_OWNER_IDS || "").trim();
  return new Set(raw.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean));
}

async function fetchDiscordJson<T>(url: string, authorization: string): Promise<{ ok: boolean; status: number; data: T | null }> {
  try {
    const response = await fetch(url, { headers: { Authorization: authorization } });
    const text = await response.text();
    let data: T | null = null;
    try {
      data = text ? JSON.parse(text) as T : null;
    } catch {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

function permissionFromRoles(memberRoles: string[], roles: Array<Record<string, unknown>>, ownerId: string | null, userId: string): bigint {
  if (ownerId && ownerId === userId) return PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD;
  let bits = 0n;
  const memberRoleSet = new Set(memberRoles);
  for (const role of roles) {
    const id = String(role.id ?? "");
    if (!memberRoleSet.has(id)) continue;
    try {
      bits |= BigInt(String(role.permissions ?? "0"));
    } catch {
      // ignore bad role permissions
    }
  }
  return bits;
}

function hasManageBits(permissionValue: unknown, owner: boolean): boolean {
  if (owner) return true;
  try {
    const bits = BigInt(String(permissionValue ?? "0"));
    return (bits & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR || (bits & PERMISSION_MANAGE_GUILD) === PERMISSION_MANAGE_GUILD;
  } catch {
    return false;
  }
}

function guildIconUrl(guildId: string, iconHash: unknown): string | null {
  if (typeof iconHash !== "string" || !iconHash.trim()) return null;
  const extension = iconHash.startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/icons/${guildId}/${iconHash}.${extension}?size=128`;
}

export function createDashboardInviteUrl(guildId?: string | null): string | null {
  const appClientId = clientId();
  if (!appClientId) return null;
  const permissions = String(process.env.DASHBOARD_BOT_INVITE_PERMISSIONS || process.env.BOT_INVITE_PERMISSIONS || "8").trim() || "8";
  const params = new URLSearchParams({
    client_id: appClientId,
    permissions,
    scope: "bot applications.commands",
  });
  if (guildId && /^\d{15,25}$/.test(guildId)) {
    params.set("guild_id", guildId);
    params.set("disable_guild_select", "true");
  }
  return `https://discord.com/oauth2/authorize?${params.toString()}`;
}

export async function getDiscordUserIdentity(accessToken: string): Promise<{ ok: boolean; status: number; user: DiscordUserIdentity | null }> {
  if (!accessToken) return { ok: false, status: 401, user: null };
  const me = await fetchDiscordJson<DiscordUserIdentity>("https://discord.com/api/v10/users/@me", `Bearer ${accessToken}`);
  if (!me.ok || !me.data || !/^\d{15,25}$/.test(String(me.data.id ?? ""))) {
    return { ok: false, status: me.status || 401, user: null };
  }
  return { ok: true, status: 200, user: me.data };
}

async function hasGuildAdminPermission(guildId: string, userId: string): Promise<{ ok: boolean; reason: string }> {
  const owners = parseAllowedOwners();
  if (owners.has(userId)) return { ok: true, reason: "owner_env" };

  const token = botToken();
  if (!token) return { ok: false, reason: "bot_token_missing" };

  const auth = `Bot ${token}`;
  const guildResp = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}`, auth);
  if (!guildResp.ok || !guildResp.data) return { ok: false, reason: `guild_fetch_failed_${guildResp.status}` };

  const ownerId = typeof guildResp.data.owner_id === "string" ? guildResp.data.owner_id : null;
  if (ownerId && ownerId === userId) return { ok: true, reason: "guild_owner" };

  const memberResp = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}/members/${userId}`, auth);
  if (!memberResp.ok || !memberResp.data) return { ok: false, reason: `member_fetch_failed_${memberResp.status}` };

  const rolesResp = await fetchDiscordJson<Array<Record<string, unknown>>>(`https://discord.com/api/v10/guilds/${guildId}/roles`, auth);
  if (!rolesResp.ok || !Array.isArray(rolesResp.data)) return { ok: false, reason: `roles_fetch_failed_${rolesResp.status}` };

  const memberRoles = Array.isArray(memberResp.data.roles) ? memberResp.data.roles.map((item) => String(item)) : [];
  const permissions = permissionFromRoles(memberRoles, rolesResp.data, ownerId, userId);
  if ((permissions & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR) return { ok: true, reason: "administrator" };
  if ((permissions & PERMISSION_MANAGE_GUILD) === PERMISSION_MANAGE_GUILD) return { ok: true, reason: "manage_guild" };
  return { ok: false, reason: "missing_manage_guild" };
}

async function fetchBotGuildIds(): Promise<Set<string> | null> {
  const token = botToken();
  if (!token) return null;
  const response = await fetchDiscordJson<Array<Record<string, unknown>>>("https://discord.com/api/v10/users/@me/guilds", `Bot ${token}`);
  if (!response.ok || !Array.isArray(response.data)) return null;
  return new Set(response.data.map((guild) => String(guild.id ?? "")).filter((id) => /^\d{15,25}$/.test(id)));
}

async function checkBotInGuild(guildId: string, botGuildIds: Set<string> | null): Promise<boolean> {
  if (botGuildIds) return botGuildIds.has(guildId);
  const token = botToken();
  if (!token) return false;
  const response = await fetchDiscordJson<Record<string, unknown>>(`https://discord.com/api/v10/guilds/${guildId}`, `Bot ${token}`);
  return response.ok;
}

export async function listDashboardServers(accessToken: string): Promise<DashboardServerListResult> {
  const userResult = await getDiscordUserIdentity(accessToken);
  if (!userResult.ok || !userResult.user) {
    return { ok: false, status: userResult.status || 401, user: null, manageable: [], needsInvite: [], error: "user_fetch_failed" };
  }

  const guildsResp = await fetchDiscordJson<Array<Record<string, unknown>>>("https://discord.com/api/v10/users/@me/guilds", `Bearer ${accessToken}`);
  if (!guildsResp.ok || !Array.isArray(guildsResp.data)) {
    return { ok: false, status: guildsResp.status || 400, user: userResult.user, manageable: [], needsInvite: [], error: "guilds_fetch_failed" };
  }

  const botGuildIds = await fetchBotGuildIds();
  const manageable: DashboardServerCard[] = [];
  const needsInvite: DashboardServerCard[] = [];

  for (const guild of guildsResp.data) {
    const id = String(guild.id ?? "");
    if (!/^\d{15,25}$/.test(id)) continue;

    const owner = guild.owner === true;
    const canManage = hasManageBits(guild.permissions, owner);
    if (!canManage) continue;

    const botPresent = await checkBotInGuild(id, botGuildIds);
    const card: DashboardServerCard = {
      id,
      name: String(guild.name ?? `Servidor ${id.slice(-4)}`),
      icon: guildIconUrl(id, guild.icon),
      owner,
      permissions: String(guild.permissions ?? "0"),
      botPresent,
      canManage: botPresent,
      canInvite: !botPresent,
      reason: botPresent ? (owner ? "owner" : "manage_guild") : "bot_missing",
      inviteUrl: botPresent ? null : createDashboardInviteUrl(id),
    };

    if (botPresent) manageable.push(card);
    else needsInvite.push(card);
  }

  manageable.sort((left, right) => left.name.localeCompare(right.name, "pt-BR"));
  needsInvite.sort((left, right) => left.name.localeCompare(right.name, "pt-BR"));

  return { ok: true, status: 200, user: userResult.user, manageable, needsInvite };
}

export async function verifyDashboardAccess(accessToken: string, guildId: string): Promise<DashboardAccessResult> {
  if (!accessToken) return { ok: false, status: 401, user: null, reason: "missing_access_token" };
  if (!/^\d{15,25}$/.test(guildId)) return { ok: false, status: 400, user: null, reason: "invalid_guild_id" };

  const me = await getDiscordUserIdentity(accessToken);
  if (!me.ok || !me.user) {
    return { ok: false, status: 401, user: null, reason: `user_fetch_failed_${me.status}` };
  }

  const permission = await hasGuildAdminPermission(guildId, me.user.id);
  if (!permission.ok) {
    return { ok: false, status: 403, user: me.user, reason: permission.reason };
  }

  return { ok: true, status: 200, user: me.user, reason: permission.reason };
}
