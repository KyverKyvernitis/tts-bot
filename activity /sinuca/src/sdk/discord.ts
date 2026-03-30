import { DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser } from "../types/activity";

let sdk: DiscordSDK | null = null;

function buildFallbackUser(): ActivityUser {
  const params = new URLSearchParams(window.location.search);
  const queryUserId = params.get("user_id") ?? params.get("userId");
  const queryDisplay = params.get("display_name") ?? params.get("displayName");
  const seed = queryUserId ?? crypto.randomUUID();

  return {
    userId: seed,
    displayName: queryDisplay ?? `Jogador ${seed.slice(0, 4)}`,
  };
}

function readContextFromQuery(): ActivityContext {
  const params = new URLSearchParams(window.location.search);
  const guildId = params.get("guild_id");
  const channelId = params.get("channel_id");
  const instanceId = params.get("instance_id") ?? params.get("instanceId");

  return {
    mode: guildId ? "server" : "casual",
    instanceId,
    guildId,
    channelId,
    source: "query",
  };
}

export function getDiscordSdk(): DiscordSDK | null {
  if (sdk) return sdk;
  const clientId = import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined;
  if (!clientId) return null;
  sdk = new DiscordSDK(clientId);
  return sdk;
}

export async function bootstrapDiscord(): Promise<ActivityBootstrap> {
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  const context = readContextFromQuery();
  const currentUser = buildFallbackUser();
  const discord = getDiscordSdk();

  if (!discord) {
    return {
      sdkReady: false,
      clientId,
      context: { ...context, source: "fallback" },
      currentUser,
    };
  }

  try {
    await discord.ready();
    return {
      sdkReady: true,
      clientId,
      context,
      currentUser,
    };
  } catch {
    return {
      sdkReady: false,
      clientId,
      context: { ...context, source: "fallback" },
      currentUser,
    };
  }
}
