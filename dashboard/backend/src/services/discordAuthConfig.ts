export function discordBotToken(): string {
  return String(process.env.DISCORD_BOT_TOKEN || process.env.DISCORD_TOKEN || process.env.BOT_TOKEN || process.env.TOKEN || "").trim();
}

export function dashboardClientId(): string {
  return String(process.env.VITE_DISCORD_CLIENT_ID || process.env.DISCORD_CLIENT_ID || process.env.CLIENT_ID || "").trim();
}

export function dashboardAllowedOwners(): Set<string> {
  const raw = String(process.env.DASHBOARD_ADMIN_USER_IDS || process.env.OWNER_IDS || process.env.BOT_OWNER_IDS || "").trim();
  return new Set(raw.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean));
}

export function dashboardSupportInviteUrl(): string {
  return String(process.env.DASHBOARD_SUPPORT_INVITE_URL || "https://discord.gg/RckuzJbvVk").trim() || "https://discord.gg/RckuzJbvVk";
}

export function dashboardSupportInviteCode(): string {
  const configured = String(process.env.DASHBOARD_SUPPORT_INVITE_CODE || "").trim();
  if (configured) return configured;
  try {
    const inviteUrl = dashboardSupportInviteUrl();
    const url = new URL(/^https?:\/\//i.test(inviteUrl) ? inviteUrl : `https://${inviteUrl}`);
    const parts = url.pathname.split("/").filter(Boolean);
    return parts[parts.length - 1] || "RckuzJbvVk";
  } catch {
    return "RckuzJbvVk";
  }
}

export function createDashboardInviteUrl(guildId?: string | null): string | null {
  const appClientId = dashboardClientId();
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
