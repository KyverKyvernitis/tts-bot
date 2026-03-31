import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser } from "../types/activity";

let sdk: DiscordSDK | null = null;
const cachedUserStorageKey = "sinuca_activity_cached_user";
const cachedTokenStorageKey = "sinuca_activity_access_token";

type DiscordSdkWithContext = DiscordSDK & {
  guildId?: string | null;
  channelId?: string | null;
  instanceId?: string | null;
};

function isDiscordSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{17,20}$/.test(value);
}

function buildPendingUser(): ActivityUser {
  const params = new URLSearchParams(window.location.search);
  const queryUserId = params.get("user_id") ?? params.get("userId");
  const queryDisplay = params.get("display_name") ?? params.get("displayName");
  const cached = readCachedUser();
  const userId = isDiscordSnowflake(queryUserId) ? queryUserId : (isDiscordSnowflake(cached?.userId) ? cached.userId : "pending-auth");

  return {
    userId,
    displayName: queryDisplay ?? cached?.displayName ?? "Conta não identificada",
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
    const raw = window.sessionStorage.getItem(cachedUserStorageKey) ?? window.localStorage.getItem(cachedUserStorageKey);
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
    window.localStorage.setItem(cachedUserStorageKey, JSON.stringify(user));
  } catch {
    // ignore storage failures in embedded browsers
  }
}

function readCachedToken(): string | null {
  try {
    return window.localStorage.getItem(cachedTokenStorageKey) || window.sessionStorage.getItem(cachedTokenStorageKey);
  } catch {
    return null;
  }
}

function writeCachedToken(token: string) {
  try {
    window.localStorage.setItem(cachedTokenStorageKey, token);
    window.sessionStorage.setItem(cachedTokenStorageKey, token);
  } catch {
    // ignore storage failures
  }
}

async function exchangeCode(code: string): Promise<string | null> {
  try {
    const response = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await response.json() as { access_token?: string; error?: string; detail?: string | null };
    if (!response.ok || typeof data.access_token !== "string" || !data.access_token) {
      console.error("[sinuca-auth] exchange failed", data.error ?? "token_exchange_failed", data.detail ?? null);
      return null;
    }
    return data.access_token;
  } catch (error) {
    console.error("[sinuca-auth] exchange exception", error);
    return null;
  }
}

async function authenticateAccessToken(discord: DiscordSDK, accessToken: string): Promise<ActivityUser | null> {
  try {
    const authenticated = await discord.commands.authenticate({ access_token: accessToken });
    const user = (authenticated as { user?: { id?: string; global_name?: string | null; username?: string | null } }).user;
    if (!isDiscordSnowflake(user?.id)) {
      console.error("[sinuca-auth] authenticate returned invalid user", user ?? null);
      return null;
    }
    return {
      userId: user.id,
      displayName: user.global_name ?? user.username ?? `Jogador ${user.id.slice(-4)}`,
    };
  } catch {
    return null;
  }
}

function clearCachedToken() {
  try {
    window.localStorage.removeItem(cachedTokenStorageKey);
    window.sessionStorage.removeItem(cachedTokenStorageKey);
  } catch {
    // ignore storage failures
  }
}

function clearCachedUser() {
  try {
    window.localStorage.removeItem(cachedUserStorageKey);
    window.sessionStorage.removeItem(cachedUserStorageKey);
  } catch {
    // ignore storage failures
  }
}

async function authorizeAndAuthenticate(discord: DiscordSDK, clientId: string, promptMode: "none" | undefined): Promise<ActivityUser | null> {
  try {
    const authorizeInput: Record<string, unknown> = {
      client_id: clientId,
      response_type: "code",
      state: `sinuca-auth-${promptMode ?? "consent"}`,
      scope: ["identify"],
    };
    if (promptMode === "none") {
      authorizeInput.prompt = "none";
    }
    const authorize = await discord.commands.authorize(authorizeInput as never);
    const code = (authorize as { code?: string }).code;
    if (!code) {
      console.error("[sinuca-auth] authorize returned without code", { promptMode: promptMode ?? "consent" });
      return null;
    }
    const accessToken = await exchangeCode(code);
    if (!accessToken) return null;
    writeCachedToken(accessToken);
    const authenticated = await authenticateAccessToken(discord, accessToken);
    if (!authenticated) {
      console.error("[sinuca-auth] authenticate failed after token exchange");
      clearCachedToken();
      return null;
    }
    const refined = await refineDisplayNameFromParticipants(discord, authenticated);
    writeCachedUser(refined);
    return refined;
  } catch (error) {
    console.error("[sinuca-auth] authorize/authenticate exception", error);
    return null;
  }
}

async function resolveAuthenticatedUser(discord: DiscordSDK, fallback: ActivityUser, clientId: string | null): Promise<ActivityUser> {
  const cachedToken = readCachedToken();
  if (cachedToken) {
    const fromCachedToken = await authenticateAccessToken(discord, cachedToken);
    if (fromCachedToken) {
      writeCachedUser(fromCachedToken);
      return fromCachedToken;
    }
    clearCachedToken();
    clearCachedUser();
  }

  if (clientId) {
    const silentUser = await authorizeAndAuthenticate(discord, clientId, "none");
    if (silentUser) {
      return silentUser;
    }
  }

  return fallback;
}


export async function authorizeDiscordUser(): Promise<ActivityUser | null> {
  const discord = getDiscordSdk();
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  if (!discord || !clientId) return null;
  try {
    await discord.ready();
    return await authorizeAndAuthenticate(discord, clientId, undefined);
  } catch {
    return null;
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

async function refineDisplayNameFromParticipants(discord: DiscordSDK, user: ActivityUser): Promise<ActivityUser> {
  try {
    const response = await discord.commands.getInstanceConnectedParticipants();
    const participants = response?.participants ?? [];
    const candidate = participants.find((participant) => {
      const maybeUserId = String((participant as { user_id?: string }).user_id ?? "");
      return maybeUserId && maybeUserId === user.userId;
    });
    if (!candidate) return user;
    return {
      userId: user.userId,
      displayName: String((candidate as { global_name?: string; username?: string }).global_name ?? (candidate as { username?: string }).username ?? user.displayName),
    };
  } catch {
    return user;
  }
}

export async function bootstrapDiscord(): Promise<ActivityBootstrap> {
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  const queryContext = readContextFromQuery();
  const fallbackUser = buildPendingUser();
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
    const authenticatedUser = await resolveAuthenticatedUser(discord, fallbackUser, clientId);
    const currentUser = await refineDisplayNameFromParticipants(discord, authenticatedUser);
    if (isDiscordSnowflake(currentUser.userId)) {
      writeCachedUser(currentUser);
    }

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
