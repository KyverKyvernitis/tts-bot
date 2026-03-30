import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser } from "../types/activity";

let sdk: DiscordSDK | null = null;

type DiscordSdkWithContext = DiscordSDK & {
  guildId?: string | null;
  channelId?: string | null;
  instanceId?: string | null;
};

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

function mergeContext(base: ActivityContext, discord: DiscordSdkWithContext | null): ActivityContext {
  return {
    mode: discord?.guildId ?? base.guildId ? "server" : base.mode,
    guildId: discord?.guildId ?? base.guildId,
    channelId: discord?.channelId ?? base.channelId,
    instanceId: discord?.instanceId ?? base.instanceId,
    source: base.source,
  };
}

export function getDiscordSdk(): DiscordSDK | null {
  if (sdk) return sdk;
  const clientId = import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined;
  if (!clientId) return null;
  sdk = new DiscordSDK(clientId);
  return sdk;
}

async function lockLandscape(discord: DiscordSDK) {
  try {
    await discord.commands.setOrientationLockState({
      lock_state: Common.OrientationLockStateTypeObject.LANDSCAPE,
      picture_in_picture_lock_state: Common.OrientationLockStateTypeObject.LANDSCAPE,
      grid_lock_state: Common.OrientationLockStateTypeObject.LANDSCAPE,
    });
  } catch {
    // ignore orientation lock failures; portal config still acts as fallback
  }
}

async function exchangeCode(code: string): Promise<string | null> {
  const response = await fetch("/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  if (!response.ok) return null;
  const data = (await response.json()) as { access_token?: string };
  return typeof data.access_token === "string" ? data.access_token : null;
}

async function authenticateCurrentUser(discord: DiscordSDK, fallback: ActivityUser): Promise<ActivityUser> {
  try {
    const { code } = await discord.commands.authorize({
      client_id: import.meta.env.VITE_DISCORD_CLIENT_ID,
      response_type: "code",
      state: "sinuca-activity",
      prompt: "none",
      scope: ["identify", "guilds", "guilds.members.read"],
    });
    if (!code) return fallback;

    const accessToken = await exchangeCode(code);
    if (!accessToken) return fallback;

    const auth = await discord.commands.authenticate({ access_token: accessToken });
    const user = auth?.user;
    if (!user?.id) return fallback;

    return {
      userId: String(user.id),
      displayName: String(user.global_name ?? user.username ?? fallback.displayName),
    };
  } catch {
    return fallback;
  }
}

async function resolveParticipantFallback(discord: DiscordSDK, fallback: ActivityUser): Promise<ActivityUser> {
  try {
    const response = await discord.commands.getInstanceConnectedParticipants();
    const participants = response?.participants ?? [];
    if (participants.length !== 1) return fallback;
    const participant = participants[0];
    if (!participant?.id) return fallback;
    return {
      userId: String(participant.id),
      displayName: String(participant.global_name ?? participant.username ?? fallback.displayName),
    };
  } catch {
    return fallback;
  }
}

export async function bootstrapDiscord(): Promise<ActivityBootstrap> {
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  const queryContext = readContextFromQuery();
  const fallbackUser = buildFallbackUser();
  const discord = getDiscordSdk();

  if (!discord) {
    return {
      sdkReady: false,
      clientId,
      context: { ...queryContext, source: "fallback" },
      currentUser: fallbackUser,
    };
  }

  try {
    await discord.ready();
    await lockLandscape(discord);
    const context = mergeContext(queryContext, discord as DiscordSdkWithContext);
    const authenticatedUser = await authenticateCurrentUser(discord, fallbackUser);
    const currentUser = authenticatedUser.userId !== fallbackUser.userId
      ? authenticatedUser
      : await resolveParticipantFallback(discord, fallbackUser);

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
      context: { ...queryContext, source: "fallback" },
      currentUser: fallbackUser,
    };
  }
}
