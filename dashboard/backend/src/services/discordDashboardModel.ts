import {
  botBasePermissions,
  botChannelPermissions,
  channelCapabilities,
  hasManageBits,
} from "./discordPermissions.js";
import { guildIconUrl } from "./discordPresentation.js";

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

export function buildDashboardGuildOptions(
  guildId: string,
  botId: string | null,
  botRoleIds: Set<string> | null,
  channelRecords: Array<Record<string, unknown>>,
  roleRecords: Array<Record<string, unknown>>,
): { channels: DashboardChannelOption[]; roles: DashboardRoleOption[] } {
  const basePermissions = botRoleIds ? botBasePermissions(guildId, botRoleIds, roleRecords) : null;
  const channels = channelRecords
    .map((channel) => {
      const permissions = botRoleIds && botId && basePermissions !== null
        ? botChannelPermissions(channel, guildId, botId, botRoleIds, basePermissions)
        : null;
      return {
        id: String(channel.id ?? ""),
        name: String(channel.name ?? ""),
        type: Number(channel.type ?? -1),
        parentId: channel.parent_id ? String(channel.parent_id) : null,
        ...channelCapabilities(permissions),
      };
    })
    .filter((channel) => /^\d{15,25}$/.test(channel.id) && Boolean(channel.name));

  const botHighestPosition = botRoleIds
    ? roleRecords.reduce((highest, role) => botRoleIds.has(String(role.id ?? "")) ? Math.max(highest, Number(role.position ?? 0)) : highest, 0)
    : null;
  const roles = roleRecords
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
  return { channels, roles };
}

export function eligibleDashboardGuilds(guilds: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  return guilds.filter((guild) => {
    const id = String(guild.id ?? "");
    if (!/^\d{15,25}$/.test(id)) return false;
    return hasManageBits(guild.permissions, guild.owner === true);
  });
}

export function partitionDashboardServerCards(
  guilds: Array<Record<string, unknown>>,
  botPresence: boolean[],
  inviteUrlForGuild: (guildId: string) => string | null,
): { manageable: DashboardServerCard[]; needsInvite: DashboardServerCard[] } {
  const manageable: DashboardServerCard[] = [];
  const needsInvite: DashboardServerCard[] = [];
  guilds.forEach((guild, index) => {
    const id = String(guild.id ?? "");
    const owner = guild.owner === true;
    const botPresent = botPresence[index] ?? false;
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
      inviteUrl: botPresent ? null : inviteUrlForGuild(id),
    };
    if (botPresent) manageable.push(card);
    else needsInvite.push(card);
  });
  manageable.sort((left, right) => left.name.localeCompare(right.name, "pt-BR"));
  needsInvite.sort((left, right) => left.name.localeCompare(right.name, "pt-BR"));
  return { manageable, needsInvite };
}
