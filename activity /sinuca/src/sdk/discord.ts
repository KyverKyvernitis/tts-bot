import { Common, DiscordSDK } from "@discord/embedded-app-sdk";
import type { ActivityBootstrap, ActivityContext, ActivityUser, SessionContextPayload } from "../types/activity";

let sdk: DiscordSDK | null = null;
const cachedUserStorageKey = "sinuca_activity_cached_user";
const cachedTokenStorageKey = "sinuca_activity_access_token";
const pkceVerifierStorageKey = "sinuca_activity_pkce_verifier";

type DiscordSdkWithContext = DiscordSDK & {
  guildId?: string | null;
  channelId?: string | null;
  instanceId?: string | null;
};

function isDiscordSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{17,20}$/.test(value);
}

function normalizeIntString(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
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

async function exchangeCode(code: string, codeVerifier: string | null): Promise<string | null> {
  try {
    const response = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, code_verifier: codeVerifier }),
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

function readPkceVerifier(): string | null {
  try {
    return window.sessionStorage.getItem(pkceVerifierStorageKey) || window.localStorage.getItem(pkceVerifierStorageKey);
  } catch {
    return null;
  }
}

function writePkceVerifier(verifier: string) {
  try {
    window.sessionStorage.setItem(pkceVerifierStorageKey, verifier);
    window.localStorage.setItem(pkceVerifierStorageKey, verifier);
  } catch {
    // ignore storage failures
  }
}

function clearPkceVerifier() {
  try {
    window.sessionStorage.removeItem(pkceVerifierStorageKey);
    window.localStorage.removeItem(pkceVerifierStorageKey);
  } catch {
    // ignore storage failures
  }
}

function base64Url(bytes: Uint8Array): string {
  const binary = Array.from(bytes).map((b) => String.fromCharCode(b)).join('');
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

async function createPkcePair(): Promise<{ verifier: string; challenge: string }> {
  const verifierBytes = crypto.getRandomValues(new Uint8Array(32));
  const verifier = base64Url(verifierBytes);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const challenge = base64Url(new Uint8Array(digest));
  return { verifier, challenge };
}

async function fetchProxySessionContext(): Promise<SessionContextPayload | null> {
  try {
    const response = await fetch("/api/session", {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
      credentials: "include",
    });
    if (!response.ok) {
      console.error("[sinuca-auth] proxy session failed", response.status);
      return null;
    }
    const data = await response.json() as SessionContextPayload & { proxyPayload?: string };
    const hasContext = Boolean(data.userId || data.guildId || data.channelId || data.instanceId);
    console.error("[sinuca-auth] proxy session", data);
    return hasContext ? {
      userId: normalizeIntString(data.userId),
      displayName: typeof data.displayName === 'string' && data.displayName.trim() ? data.displayName.trim() : null,
      guildId: normalizeIntString(data.guildId),
      channelId: normalizeIntString(data.channelId),
      instanceId: normalizeIntString(data.instanceId),
    } : null;
  } catch {
    return null;
  }
}


async function authorizeAndAuthenticate(discord: DiscordSDK, clientId: string, promptMode: "none" | undefined): Promise<ActivityUser | null> {
  try {
    const pkce = await createPkcePair();
    writePkceVerifier(pkce.verifier);
    const authorizeInput: Record<string, unknown> = {
      client_id: clientId,
      response_type: "code",
      state: `sinuca-auth-${promptMode ?? "consent"}`,
      scope: ["identify"],
      code_challenge: pkce.challenge,
      code_challenge_method: "S256",
    };
    if (promptMode === "none") {
      authorizeInput.prompt = "none";
    }
    const authorize = await discord.commands.authorize(authorizeInput as never);
    const code = (authorize as { code?: string }).code;
    if (!code) {
      console.error("[sinuca-auth] authorize returned without code", { promptMode: promptMode ?? "consent" });
      clearPkceVerifier();
      return null;
    }
    const verifier = readPkceVerifier();
    const accessToken = await exchangeCode(code, verifier);
    clearPkceVerifier();
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
    clearPkceVerifier();
    console.error("[sinuca-auth] authorize/authenticate exception", error);
    return null;
  }
}

async function resolveAuthenticatedUser(discord: DiscordSDK, fallback: ActivityUser, proxySession: SessionContextPayload | null, bootDebug: string[]): Promise<ActivityUser> {
  if (isDiscordSnowflake(proxySession?.userId)) {
    bootDebug.push("proxy-session:user-ok");
    const userFromProxy: ActivityUser = {
      userId: proxySession.userId,
      displayName: proxySession?.displayName ?? fallback.displayName ?? `Jogador ${proxySession.userId.slice(-4)}`,
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

  try {
    const response = await discord.commands.getInstanceConnectedParticipants();
    const participants = response?.participants ?? [];
    bootDebug.push(`participants:${participants.length}`);
    if (participants.length === 1) {
      const participant = participants[0] as { id?: string; global_name?: string | null; username?: string | null };
      if (isDiscordSnowflake(participant.id)) {
        bootDebug.push("participants:single-user-ok");
        const singleUser = { userId: participant.id, displayName: participant.global_name ?? participant.username ?? fallback.displayName };
        writeCachedUser(singleUser);
        return singleUser;
      }
    }
  } catch (error) {
    bootDebug.push(`participants:error:${error instanceof Error ? error.message : "unknown"}`);
    console.error("[sinuca-auth] getInstanceConnectedParticipants failed", error);
  }

  bootDebug.push("user:fallback-pending-auth");
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
    bootDebug.push(`sdk:ready:error:${error instanceof Error ? error.message : "unknown"}`);
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
    proxySession = await fetchProxySessionContext();
    bootDebug.push(proxySession ? `proxy-session:ok:${proxySession.userId ?? "no-user"}:${proxySession.guildId ?? "no-guild"}` : "proxy-session:empty");
  } catch (error) {
    bootDebug.push(`proxy-session:error:${error instanceof Error ? error.message : "unknown"}`);
  }

  const mergedContext = mergeContext(queryContext, discord as DiscordSdkWithContext);
  const context = {
    ...mergedContext,
    guildId: proxySession?.guildId ?? mergedContext.guildId,
    channelId: proxySession?.channelId ?? mergedContext.channelId,
    instanceId: proxySession?.instanceId ?? mergedContext.instanceId,
    mode: (proxySession?.guildId ?? mergedContext.guildId) ? "server" : mergedContext.mode,
  };

  const authenticatedUser = await resolveAuthenticatedUser(discord, fallbackUser, proxySession, bootDebug);
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
