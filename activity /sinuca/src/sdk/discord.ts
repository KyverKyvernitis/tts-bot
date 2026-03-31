import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser, SessionContextPayload } from "../types/activity";

interface ProxySessionResult {
  session: SessionContextPayload | null;
  debug: string[];
}

interface AuthorizeResult {
  user: ActivityUser | null;
  debug: string;
}

interface TokenExchangeResult {
  accessToken: string | null;
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

function isDiscordSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{17,20}$/.test(value);
}

function normalizeIntString(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "unknown";
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

async function exchangeCode(code: string): Promise<TokenExchangeResult> {
  try {
    const response = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const raw = await response.text();
    let data: { access_token?: string; error?: string; detail?: string | null } = {};
    try {
      data = JSON.parse(raw) as { access_token?: string; error?: string; detail?: string | null };
    } catch {
      return {
        accessToken: null,
        debug: `token:json_invalid:${response.status}:${raw.slice(0, 80) || "empty"}`,
      };
    }

    if (!response.ok || typeof data.access_token !== "string" || !data.access_token) {
      console.error("[sinuca-auth] exchange failed", data.error ?? "token_exchange_failed", data.detail ?? null);
      return {
        accessToken: null,
        debug: `token:http_${response.status}:${data.error ?? "token_exchange_failed"}`,
      };
    }

    return { accessToken: data.access_token, debug: `token:http_${response.status}:ok` };
  } catch (error) {
    console.error("[sinuca-auth] exchange exception", error);
    return { accessToken: null, debug: `token:exception:${getErrorMessage(error)}` };
  }
}

async function authenticateAccessToken(discord: DiscordSDK, accessToken: string): Promise<ActivityUser | null> {
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
    };
  } catch (error) {
    console.error("[sinuca-auth] authenticate exception", error);
    return null;
  }
}

async function fetchProxySessionContext(): Promise<ProxySessionResult> {
  const debug: string[] = [];
  try {
    const healthResponse = await fetch("/api/health", {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
      credentials: "include",
    });
    debug.push(`health:http:${healthResponse.status}`);
  } catch (error) {
    debug.push(`health:error:${getErrorMessage(error)}`);
  }

  try {
    const params = new URLSearchParams(window.location.search);
    const response = await fetch(`/api/session?${params.toString()}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
      credentials: "include",
    });
    debug.push(`session:http:${response.status}`);
    const raw = await response.text();
    debug.push(`session:text:${raw.slice(0, 120).replace(/\s+/g, " ") || "empty"}`);
    if (!response.ok) {
      console.error("[sinuca-auth] proxy session failed", response.status, raw);
      return { session: null, debug };
    }
    let data: (SessionContextPayload & { proxyPayload?: string; hasProxyPayload?: boolean; sessionSource?: string }) | null = null;
    try {
      data = JSON.parse(raw) as SessionContextPayload & { proxyPayload?: string; hasProxyPayload?: boolean; sessionSource?: string };
    } catch (error) {
      debug.push(`session:json:error:${getErrorMessage(error)}`);
      return { session: null, debug };
    }
    const hasContext = Boolean(data?.userId || data?.guildId || data?.channelId || data?.instanceId);
    debug.push(`session:proxy:${data?.proxyPayload ?? (data?.hasProxyPayload ? "present" : "unknown")}`);
    debug.push(`session:source:${data?.sessionSource ?? "unknown"}`);
    debug.push(`session:user:${data?.userId ?? "null"}`);
    debug.push(`session:guild:${data?.guildId ?? "null"}`);
    console.error("[sinuca-auth] proxy session", data);
    return {
      session: hasContext ? {
        userId: normalizeIntString(data?.userId),
        displayName: typeof data?.displayName === "string" && data.displayName.trim() ? data.displayName.trim() : null,
        guildId: normalizeIntString(data?.guildId),
        channelId: normalizeIntString(data?.channelId),
        instanceId: normalizeIntString(data?.instanceId),
      } : null,
      debug,
    };
  } catch (error) {
    debug.push(`session:error:${getErrorMessage(error)}`);
    return { session: null, debug };
  }
}

async function authorizeAndAuthenticate(
  discord: DiscordSDK,
  clientId: string,
  promptMode: AuthorizePromptMode,
): Promise<AuthorizeResult> {
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
      return { user: null, debug: `authorize:no_code:${promptMode}` };
    }

    const tokenResult = await exchangeCode(code);
    if (!tokenResult.accessToken) {
      return { user: null, debug: `authorize:exchange_failed:${promptMode}:${tokenResult.debug}` };
    }

    writeCachedToken(tokenResult.accessToken);
    const authenticated = await authenticateAccessToken(discord, tokenResult.accessToken);
    if (!authenticated) {
      console.error("[sinuca-auth] authenticate failed after token exchange");
      clearCachedToken();
      clearCachedUser();
      return { user: null, debug: `authorize:authenticate_failed:${promptMode}` };
    }

    const refined = await refineDisplayNameFromParticipants(discord, authenticated);
    writeCachedUser(refined);
    return { user: refined, debug: `authorize:ok:${promptMode}` };
  } catch (error) {
    console.error("[sinuca-auth] authorize/authenticate exception", error);
    return { user: null, debug: `authorize:exception:${promptMode}:${getErrorMessage(error)}` };
  }
}

async function resolveAuthenticatedUser(
  discord: DiscordSDK,
  clientId: string | null,
  fallback: ActivityUser,
  proxySession: SessionContextPayload | null,
  bootDebug: string[],
): Promise<ActivityUser> {
  if (isDiscordSnowflake(proxySession?.userId)) {
    bootDebug.push("proxy-session:user-ok");
    const userFromProxy: ActivityUser = {
      userId: proxySession.userId,
      displayName: proxySession.displayName ?? fallback.displayName ?? `Jogador ${proxySession.userId.slice(-4)}`,
    };
    writeCachedUser(userFromProxy);
    return userFromProxy;
  }

  const cachedToken = readCachedToken();
  if (cachedToken) {
    bootDebug.push("cached-token:found");
    const fromCachedToken = await authenticateAccessToken(discord, cachedToken);
    if (fromCachedToken) {
      bootDebug.push("cached-token:auth-ok");
      writeCachedUser(fromCachedToken);
      return fromCachedToken;
    }
    bootDebug.push("cached-token:auth-failed");
    clearCachedToken();
    clearCachedUser();
  } else {
    bootDebug.push("cached-token:none");
  }

  if (clientId) {
    bootDebug.push("authorize:none:start");
    const silentAuth = await authorizeAndAuthenticate(discord, clientId, "none");
    bootDebug.push(silentAuth.debug);
    if (silentAuth.user && isDiscordSnowflake(silentAuth.user.userId)) {
      bootDebug.push("authorize:none:user-ok");
      return silentAuth.user;
    }
  } else {
    bootDebug.push("authorize:none:skipped:no_client_id");
  }

  bootDebug.push("participants:skipped_for_identity");
  bootDebug.push("user:fallback-pending-auth");
  return fallback;
}

export async function authorizeDiscordUser(): Promise<AuthorizeResult> {
  const discord = getDiscordSdk();
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined) ?? null;
  if (!discord || !clientId) return { user: null, debug: "authorize:sdk_or_client_missing" };
  try {
    await discord.ready();
    return await authorizeAndAuthenticate(discord, clientId, "consent");
  } catch (error) {
    return { user: null, debug: `authorize:ready_failed:${getErrorMessage(error)}` };
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

  let proxySession: SessionContextPayload | null = null;
  try {
    bootDebug.push("proxy-session:start");
    const proxyResult = await fetchProxySessionContext();
    proxySession = proxyResult.session;
    bootDebug.push(...proxyResult.debug.map((entry) => `proxy:${entry}`));
    bootDebug.push(proxySession ? `proxy-session:ok:${proxySession.userId ?? "no-user"}:${proxySession.guildId ?? "no-guild"}` : "proxy-session:empty");
  } catch (error) {
    bootDebug.push(`proxy-session:error:${getErrorMessage(error)}`);
  }

  const mergedContext = mergeContext(queryContext, discord as DiscordSdkWithContext);
  const context = {
    ...mergedContext,
    guildId: proxySession?.guildId ?? mergedContext.guildId,
    channelId: proxySession?.channelId ?? mergedContext.channelId,
    instanceId: proxySession?.instanceId ?? mergedContext.instanceId,
    mode: (proxySession?.guildId ?? mergedContext.guildId) ? "server" : mergedContext.mode,
  };

  const authenticatedUser = await resolveAuthenticatedUser(discord, clientId, fallbackUser, proxySession, bootDebug);
  const baseUser = proxySession?.displayName && isDiscordSnowflake(authenticatedUser.userId)
    ? { ...authenticatedUser, displayName: proxySession.displayName }
    : authenticatedUser;
  const currentUser = await refineDisplayNameFromParticipants(discord, baseUser);
  if (isDiscordSnowflake(currentUser.userId)) {
    writeCachedUser(currentUser);
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
