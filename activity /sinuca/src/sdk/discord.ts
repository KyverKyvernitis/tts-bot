import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser } from "../types/activity";

let sdk: DiscordSDK | null = null;
const cachedUserStorageKey = "sinuca_activity_cached_user";

type DiscordSdkWithContext = DiscordSDK & {
  guildId?: string | null;
  channelId?: string | null;
  instanceId?: string | null;
};

function buildFallbackUser(): ActivityUser {
  const params = new URLSearchParams(window.location.search);
  const queryUserId = params.get("user_id") ?? params.get("userId");
  const queryDisplay = params.get("display_name") ?? params.get("displayName");
  const cached = readCachedUser();
  const seed = queryUserId ?? cached?.userId ?? crypto.randomUUID();

  return {
    userId: seed,
    displayName: queryDisplay ?? cached?.displayName ?? `Jogador ${seed.slice(0, 4)}`,
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

function mergeContext(queryContext: ActivityContext, discord: DiscordSdkWithContext | null): ActivityContext {
  return {
    mode: queryContext.guildId ? "server" : (discord?.guildId ? "server" : queryContext.mode),
    guildId: queryContext.guildId ?? discord?.guildId ?? null,
    channelId: queryContext.channelId ?? discord?.channelId ?? null,
    instanceId: queryContext.instanceId ?? discord?.instanceId ?? null,
    source: queryContext.source,
  };
}

function readCachedUser(): ActivityUser | null {
  try {
    const raw = window.sessionStorage.getItem(cachedUserStorageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ActivityUser>;
    if (typeof parsed.userId !== "string" || !parsed.userId.trim()) return null;
    return {
      userId: parsed.userId,
      displayName: typeof parsed.displayName === "string" && parsed.displayName.trim() ? parsed.displayName : `Jogador ${parsed.userId.slice(0, 4)}`,
    };
  } catch {
    return null;
  }
}

function writeCachedUser(user: ActivityUser) {
  try {
    window.sessionStorage.setItem(cachedUserStorageKey, JSON.stringify(user));
  } catch {
    // ignore sessionStorage failures in embedded browsers
  }
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

async function resolveParticipantUser(discord: DiscordSDK, fallback: ActivityUser): Promise<ActivityUser> {
  try {
    const response = await discord.commands.getInstanceConnectedParticipants();
    const participants = response?.participants ?? [];

    const cached = readCachedUser();
    if (cached) {
      const matched = participants.find((participant) => String(participant.id) === cached.userId);
      if (matched?.id) {
        return {
          userId: String(matched.id),
          displayName: String(matched.global_name ?? matched.username ?? cached.displayName),
        };
      }
    }

    const firstValid = participants.find((participant) => participant?.id);
    if (firstValid?.id) {
      return {
        userId: String(firstValid.id),
        displayName: String(firstValid.global_name ?? firstValid.username ?? fallback.displayName),
      };
    }
  } catch {
    // ignore participant lookup failures and keep fallback user
  }
  return fallback;
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
    const currentUser = await resolveParticipantUser(discord, fallbackUser);
    writeCachedUser(currentUser);

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
