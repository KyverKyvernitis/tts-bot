export const PERMISSION_ADMINISTRATOR = 0x0000000000000008n;
export const PERMISSION_MANAGE_GUILD = 0x0000000000000020n;
export const PERMISSION_MANAGE_CHANNELS = 1n << 4n;
export const PERMISSION_VIEW_CHANNEL = 1n << 10n;
export const PERMISSION_SEND_MESSAGES = 1n << 11n;
export const PERMISSION_CONNECT = 1n << 20n;
export const PERMISSION_MANAGE_WEBHOOKS = 1n << 29n;

export function permissionBits(value: unknown): bigint {
  try {
    return BigInt(String(value ?? "0"));
  } catch {
    return 0n;
  }
}

export function botBasePermissions(guildId: string, botRoleIds: Set<string>, roles: Array<Record<string, unknown>>): bigint {
  let permissions = 0n;
  for (const role of roles) {
    const roleId = String(role.id ?? "");
    if (roleId !== guildId && !botRoleIds.has(roleId)) continue;
    permissions |= permissionBits(role.permissions);
  }
  return permissions;
}

export function applyPermissionOverwrite(permissions: bigint, overwrite: Record<string, unknown>): bigint {
  return (permissions & ~permissionBits(overwrite.deny)) | permissionBits(overwrite.allow);
}

export function botChannelPermissions(
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

export function permissionFromRoles(memberRoles: string[], roles: Array<Record<string, unknown>>, ownerId: string | null, userId: string, guildId?: string): bigint {
  if (ownerId && ownerId === userId) return PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD;
  let bits = 0n;
  const memberRoleSet = new Set(memberRoles);
  for (const role of roles) {
    const id = String(role.id ?? "");
    const everyone = id === guildId || String(role.name ?? "") === "@everyone";
    if (!everyone && !memberRoleSet.has(id)) continue;
    bits |= permissionBits(role.permissions);
  }
  return bits;
}

export function hasManageBits(permissionValue: unknown, owner: boolean): boolean {
  if (owner) return true;
  const bits = permissionBits(permissionValue);
  return (bits & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR || (bits & PERMISSION_MANAGE_GUILD) === PERMISSION_MANAGE_GUILD;
}

export function userCanManageGuild(guilds: Array<Record<string, unknown>>, guildId: string): boolean {
  const guild = guilds.find((item) => String(item.id ?? "") === guildId);
  return Boolean(guild && hasManageBits(guild.permissions, guild.owner === true));
}

export interface ChannelCapabilities {
  permissionsKnown: boolean;
  viewable: boolean;
  sendable: boolean;
  connectable: boolean;
  manageable: boolean;
  webhookManageable: boolean;
}

export function channelCapabilities(permissions: bigint | null): ChannelCapabilities {
  if (permissions === null) {
    return {
      permissionsKnown: false,
      viewable: true,
      sendable: true,
      connectable: true,
      manageable: true,
      webhookManageable: true,
    };
  }
  const admin = (permissions & PERMISSION_ADMINISTRATOR) === PERMISSION_ADMINISTRATOR;
  const has = (bit: bigint) => admin || (permissions & bit) === bit;
  return {
    permissionsKnown: true,
    viewable: has(PERMISSION_VIEW_CHANNEL),
    sendable: has(PERMISSION_VIEW_CHANNEL) && has(PERMISSION_SEND_MESSAGES),
    connectable: has(PERMISSION_VIEW_CHANNEL) && has(PERMISSION_CONNECT),
    manageable: has(PERMISSION_MANAGE_CHANNELS),
    webhookManageable: has(PERMISSION_MANAGE_WEBHOOKS),
  };
}
