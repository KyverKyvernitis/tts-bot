import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser } from "../types/activity";

interface AuthorizeCodeResult {
  code: string | null;
  debug: string;
}

type AuthorizePromptMode = "none" | "consent";

type DiscordSdkWithContext = DiscordSDK & {
  guildId?: string | null;
  channelId?: string | null;
  instanceId?: string | null;
};

let sdk: DiscordSDK | null = null;
const cachedUserStorageKey = "sinuca_activity_cached_user";
const cachedTokenStorageKey = "sinuca_activity_access_token";

export function getOAuthRedirectUri(): string | null {
  const configured = (import.meta.env.VITE_DISCORD_REDIRECT_URI as string | undefined)?.trim();
  if (configured) return configured;
  if (typeof window === "undefined") return null;
  const currentHref = window.location.href;
  if (!currentHref) return null;
  try {
    return new URL("/", currentHref).toString();
  } catch {
    return window.location.origin || null;
  }
}

function isDiscordSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{17,20}$/.test(value);
}

function buildDiscordAvatarUrl(userId: string, avatarHash: string | null | undefined): string | null {
  if (avatarHash && avatarHash.trim()) {
    return `https://cdn.discordapp.com/avatars/${userId}/${avatarHash}.png?size=128`;
  }
  try {
    const index = Number((BigInt(userId) >> 22n) % 6n);
    return `https://cdn.discordapp.com/embed/avatars/${index}.png`;
  } catch {
    return null;
  }
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    const bits = [error.name, error.message].filter(Boolean);
    return bits.length ? bits.join(":") : "error";
  }
  if (typeof error === "string" && error.trim()) return error;
  if (typeof error === "object" && error !== null) {
    const record = error as Record<string, unknown>;
    const pieces: string[] = [];
    for (const key of ["code", "message", "error", "name", "type", "status", "detail"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) pieces.push(`${key}=${value}`);
      else if (typeof value === "number" || typeof value === "boolean") pieces.push(`${key}=${String(value)}`);
    }
    if (pieces.length) return pieces.join(",");
    try {
      const raw = JSON.stringify(error);
      if (raw && raw !== "{}") return raw;
    } catch {
      // ignore stringify failures
    }
  }
  return "unknown";
}

function buildPendingUser(): ActivityUser {
  const params = new URLSearchParams(window.location.search);
  const queryUserId = params.get("user_id") ?? params.get("userId");
  const queryDisplay = params.get("display_name") ?? params.get("displayName");
  const cached = readCachedUser();
  const userId = isDiscordSnowflake(queryUserId)
    ? queryUserId
    : (isDiscordSnowflake(cached?.userId) ? cached.userId : "pending-auth");

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

export function writeCachedUser(user: ActivityUser) {
  try {
    window.sessionStorage.setItem(cachedUserStorageKey, JSON.stringify(user));
    window.localStorage.setItem(cachedUserStorageKey, JSON.stringify(user));
  } catch {
    // ignore storage failures in embedded browsers
  }
}

export function readCachedToken(): string | null {
  try {
    return window.localStorage.getItem(cachedTokenStorageKey) || window.sessionStorage.getItem(cachedTokenStorageKey);
  } catch {
    return null;
  }
}

export function writeCachedToken(token: string) {
  try {
    window.localStorage.setItem(cachedTokenStorageKey, token);
    window.sessionStorage.setItem(cachedTokenStorageKey, token);
  } catch {
    // ignore storage failures
  }
}

export function clearCachedToken() {
  try {
    window.localStorage.removeItem(cachedTokenStorageKey);
    window.sessionStorage.removeItem(cachedTokenStorageKey);
  } catch {
    // ignore storage failures
  }
}

export function clearCachedUser() {
  try {
    window.localStorage.removeItem(cachedUserStorageKey);
    window.sessionStorage.removeItem(cachedUserStorageKey);
  } catch {
    // ignore storage failures
  }
}

export async function authenticateDiscordAccessToken(discord: DiscordSDK, accessToken: string): Promise<ActivityUser | null> {
  try {
    const authenticated = await discord.commands.authenticate({ access_token: accessToken });
    if (!authenticated) {
      console.error("[sinuca-auth] authenticate returned null");
      return null;
    }
    const user = (authenticated as { user?: { id?: string; global_name?: string | null; username?: string | null } }).user;
    if (!isDiscordSnowflake(user?.id)) {
      console.error("[sinuca-auth] authenticate returned invalid user", user ?? null);
      return null;
    }
    return {
      userId: user.id,
      displayName: user.global_name ?? user.username ?? `Jogador ${user.id.slice(-4)}`,
      avatarUrl: buildDiscordAvatarUrl(user.id, (user as { avatar?: string | null }).avatar ?? null),
    };
  } catch (error) {
    console.error("[sinuca-auth] authenticate exception", error);
    return null;
  }
}

export async function authorizeDiscordCode(promptMode: AuthorizePromptMode): Promise<AuthorizeCodeResult> {
  const discord = getDiscordSdk();
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  if (!discord || !clientId) return { code: null, debug: "authorize:sdk_or_client_missing" };

  try {
    const authorize = await discord.commands.authorize({
      client_id: clientId,
      response_type: "code",
      state: `sinuca-auth-${promptMode}`,
      prompt: promptMode,
      scope: ["identify"],
    } as never);

    const code = (authorize as { code?: string | null }).code ?? null;
    if (!code) {
      console.error("[sinuca-auth] authorize returned without code", { promptMode });
      return { code: null, debug: `authorize:no_code:${promptMode}` };
    }

    return { code, debug: `authorize:code_ok:${promptMode}` };
  } catch (error) {
    console.error("[sinuca-auth] authorize exception", { error, promptMode });
    return { code: null, debug: `authorize:exception:${promptMode}:${getErrorMessage(error)}` };
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
    const participants = (response?.participants ?? []) as Array<Record<string, unknown>>;
    const candidate = participants.find((participant: Record<string, unknown>) => {
      const maybeUserId = String((participant as { id?: string; user_id?: string }).id ?? (participant as { user_id?: string }).user_id ?? "");
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

async function resolveAuthenticatedUser(
  discord: DiscordSDK,
  fallback: ActivityUser,
  bootDebug: string[],
): Promise<ActivityUser> {
  const cachedToken = readCachedToken();
  if (cachedToken) {
    bootDebug.push("cached-token:found");
    const fromCachedToken = await authenticateDiscordAccessToken(discord, cachedToken);
    if (fromCachedToken) {
      bootDebug.push("cached-token:auth-ok");
      const refined = await refineDisplayNameFromParticipants(discord, fromCachedToken);
      writeCachedUser(refined);
      return refined;
    }
    bootDebug.push("cached-token:auth-failed");
    clearCachedToken();
    clearCachedUser();
  } else {
    bootDebug.push("cached-token:none");
  }

  bootDebug.push("session-http:deferred");
  bootDebug.push("user:fallback-pending-auth");
  return fallback;
}

export async function bootstrapDiscord(): Promise<ActivityBootstrap> {
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  const queryContext = readContextFromQuery();
  const fallbackUser = buildPendingUser();
  const discord = getDiscordSdk();
  const bootDebug: string[] = [];

  if (!discord) {
    bootDebug.push("sdk:missing");
    return {
      sdkReady: false,
      clientId,
      context: { ...queryContext, source: "fallback" },
      currentUser: fallbackUser,
      bootDebug,
    };
  }

  try {
    bootDebug.push("sdk:ready:start");
    await discord.ready();
    bootDebug.push("sdk:ready:ok");
  } catch (error) {
    bootDebug.push(`sdk:ready:error:${getErrorMessage(error)}`);
    return {
      sdkReady: false,
      clientId,
      context: { ...queryContext, source: "fallback" },
      currentUser: fallbackUser,
      bootDebug,
    };
  }

  await lockLandscape(discord);
  bootDebug.push("orientation:done");

  const context = mergeContext(queryContext, discord as DiscordSdkWithContext);
  const currentUser = await resolveAuthenticatedUser(discord, fallbackUser, bootDebug);
  if (isDiscordSnowflake(currentUser.userId)) {
    bootDebug.push(`current-user:resolved:${currentUser.userId}`);
  } else {
    bootDebug.push(`current-user:pending:${currentUser.userId}`);
  }

  return {
    sdkReady: true,
    clientId,
    context,
    currentUser,
    bootDebug,
  };
}
