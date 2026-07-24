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
const PERMISSION_MANAGE_CHANNELS = 1n << 4n;
const PERMISSION_VIEW_CHANNEL = 1n << 10n;
const PERMISSION_SEND_MESSAGES = 1n << 11n;
const PERMISSION_CONNECT = 1n << 20n;
const PERMISSION_MANAGE_WEBHOOKS = 1n << 29n;
const PERMISSION_CREATE_PUBLIC_THREADS = 1n << 35n;
const PERMISSION_SEND_MESSAGES_IN_THREADS = 1n << 38n;

function permissionBits(value: unknown): bigint {
  try {
    return BigInt(String(value ?? "0"));
  } catch {
    return 0n;
  }
}

function botBasePermissions(guildId: string, botRoleIds: Set<string>, roles: Array<Record<string, unknown>>): bigint {
  let permissions = 0n;
  for (const role of roles) {
    const roleId = String(role.id ?? "");
    if (roleId !== guildId && !botRoleIds.has(roleId)) continue;
    permissions |= permissionBits(role.permissions);
  }
  return permissions;
}

function applyPermissionOverwrite(permissions: bigint, overwrite: Record<string, unknown>): bigint {
  return (permissions & ~permissionBits(overwrite.deny)) | permissionBits(overwrite.allow);
}

function botChannelPermissions(
  channel: Record<string, unknown>,
  guildId: string,
  botId: string,
  botRoleIds: Set<string>,
  basePermissions: bigint,
): bigint {
  if ((basePermissions & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR) return basePermissions;
  const overwrites = Array.isArray(channel.permission_overwrites)
    ? channel.permission_overwrites.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];

  let permissions = basePermissions;
  const everyone = overwrites.find((overwrite) => String(overwrite.id ?? "") === guildId && Number(overwrite.type ?? 0) === 0);
  if (everyone) permissions = applyPermissionOverwrite(permissions, everyone);

  let roleAllow = 0n;
  let roleDeny = 0n;
  for (const overwrite of overwrites) {
    if (Number(overwrite.type ?? 0) !== 0 || !botRoleIds.has(String(overwrite.id ?? ""))) continue;
    roleAllow |= permissionBits(overwrite.allow);
    roleDeny |= permissionBits(overwrite.deny);
  }
  permissions = (permissions & ~roleDeny) | roleAllow;

  const member = overwrites.find((overwrite) => String(overwrite.id ?? "") === botId && Number(overwrite.type ?? 0) === 1);
  if (member) permissions = applyPermissionOverwrite(permissions, member);
  return permissions;
}

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

async function fetchDiscordPublicJson<T>(url: string): Promise<{ ok: boolean; status: number; data: T | null }> {
  try {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        "User-Agent": "OsakaDashboard/2.0",
      },
    });
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

export function discordAvatarUrl(userId: unknown, avatarHash: unknown): string | null {
  if (typeof avatarHash !== "string" || !avatarHash.trim()) return null;
  const id = String(userId ?? "").trim();
  if (!id) return null;
  const extension = avatarHash.startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/avatars/${id}/${avatarHash}.${extension}?size=128`;
}

function withAvatarUrl<T extends { id: string; avatar?: string | null }>(user: T | null): (T & { avatarUrl: string | null }) | null {
  if (!user) return null;
  return { ...user, avatarUrl: discordAvatarUrl(user.id, user.avatar) };
}



let botIdentityCache: { expiresAt: number; value: DiscordUserIdentity | null } | null = null;

export async function getDiscordBotIdentity(): Promise<DiscordUserIdentity | null> {
  const token = botToken();
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

function supportInviteUrl(): string {
  return String(process.env.DASHBOARD_SUPPORT_INVITE_URL || "https://discord.gg/RckuzJbvVk").trim() || "https://discord.gg/RckuzJbvVk";
}

function supportInviteCode(): string {
  const configured = String(process.env.DASHBOARD_SUPPORT_INVITE_CODE || "").trim();
  if (configured) return configured;
  try {
    const url = new URL(/^https?:\/\//i.test(supportInviteUrl()) ? supportInviteUrl() : `https://${supportInviteUrl()}`);
    const parts = url.pathname.split("/").filter(Boolean);
    return parts[parts.length - 1] || "RckuzJbvVk";
  } catch {
    return "RckuzJbvVk";
  }
}

let supportServerCache: { expiresAt: number; value: DiscordSupportServerIdentity | null } | null = null;

export async function getDiscordSupportServerIdentity(): Promise<DiscordSupportServerIdentity | null> {
  const now = Date.now();
  if (supportServerCache && supportServerCache.expiresAt > now) return supportServerCache.value;

  const code = supportInviteCode();
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
        inviteUrl: supportInviteUrl(),
      }
    : null;

  supportServerCache = { expiresAt: now + 30 * 60 * 1000, value };
  return value;
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
  return { ok: true, status: 200, user: withAvatarUrl(me.data) };
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

export interface DashboardChannelOption {
  id: string;
  name: string;
  type: number;
  parentId: string | null;
  permissionsKnown: boolean;
  viewable: boolean;
  sendable: boolean;
  connectable: boolean;
  manageable: boolean;
  webhookManageable: boolean;
}

export interface DashboardRoleOption {
  id: string;
  name: string;
  color: number;
  managed: boolean;
  position: number;
  assignable: boolean;
}

export interface DashboardGuildOptionsResult {
  ok: boolean;
  channels: DashboardChannelOption[];
  roles: DashboardRoleOption[];
  error?: string;
}

/**
 * Canais e cargos reais do servidor, usados para trocar campos de ID manual
 * por seletores no dashboard. Usa o mesmo bot token já utilizado para checar
 * permissões — não adiciona nenhuma credencial nova. Se faltar o bot token,
 * retorna ok:false com a razão (o frontend mantém o input manual nesse caso).
 */
export async function listGuildChannelsAndRoles(guildId: string): Promise<DashboardGuildOptionsResult> {
  const token = botToken();
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
  const basePermissions = botRoleIds ? botBasePermissions(guildId, botRoleIds, rolesResp.data) : null;

  const channels: DashboardChannelOption[] = channelsResp.data
    .map((channel) => {
      const type = Number(channel.type ?? -1);
      const permissions = botRoleIds && botId && basePermissions !== null
        ? botChannelPermissions(channel, guildId, botId, botRoleIds, basePermissions)
        : null;
      const administrator = permissions !== null && (permissions & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR;
      const viewable = administrator || permissions !== null && (permissions & PERMISSION_VIEW_CHANNEL) === PERMISSION_VIEW_CHANNEL;
      const textSendable = administrator || permissions !== null && (permissions & PERMISSION_SEND_MESSAGES) === PERMISSION_SEND_MESSAGES;
      const threadSendable = administrator || permissions !== null
        && (permissions & PERMISSION_CREATE_PUBLIC_THREADS) === PERMISSION_CREATE_PUBLIC_THREADS
        && (permissions & PERMISSION_SEND_MESSAGES_IN_THREADS) === PERMISSION_SEND_MESSAGES_IN_THREADS;
      return {
        id: String(channel.id ?? ""),
        name: String(channel.name ?? ""),
        type,
        parentId: channel.parent_id ? String(channel.parent_id) : null,
        permissionsKnown: permissions !== null,
        viewable,
        sendable: viewable && ([15, 16].includes(type) ? threadSendable : textSendable),
        connectable: viewable && (administrator || permissions !== null && (permissions & PERMISSION_CONNECT) === PERMISSION_CONNECT),
        manageable: viewable && (administrator || permissions !== null && (permissions & PERMISSION_MANAGE_CHANNELS) === PERMISSION_MANAGE_CHANNELS),
        webhookManageable: viewable && (administrator || permissions !== null && (permissions & PERMISSION_MANAGE_WEBHOOKS) === PERMISSION_MANAGE_WEBHOOKS),
      };
    })
    .filter((channel) => /^\d{15,25}$/.test(channel.id) && channel.name);

  const botHighestPosition = botRoleIds
    ? rolesResp.data.reduce((highest, role) => botRoleIds.has(String(role.id ?? "")) ? Math.max(highest, Number(role.position ?? 0)) : highest, 0)
    : null;

  const roles: DashboardRoleOption[] = rolesResp.data
    .map((role) => {
      const managed = Boolean(role.managed);
      const position = Number(role.position ?? 0);
      return {
        id: String(role.id ?? ""),
        name: String(role.name ?? ""),
        color: Number(role.color ?? 0),
        managed,
        position,
        assignable: !managed && (botHighestPosition === null || position < botHighestPosition),
      };
    })
    .filter((role) => /^\d{15,25}$/.test(role.id) && role.id !== guildId && role.name !== "@everyone");

  return { ok: true, channels, roles };
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
