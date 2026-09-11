import {
  DASHBOARD_SESSION_COOKIE,
  createDashboardSessionId,
  dashboardCookieValue,
  dashboardSessionHash,
  decryptDashboardSessionText,
  deriveDashboardSessionKey,
  encryptDashboardSessionText,
  serializeDashboardCookie,
} from "./dashboardSessionPrimitives.js";
import {
  dashboardAccessExpiresAt,
  dashboardNextRefreshToken,
  dashboardSessionExpired,
  dashboardSessionNeedsRefresh,
} from "./dashboardSessionModel.js";
import { runSingleFlight } from "./singleFlight.js";
import type {
  DashboardOAuthTokenResult,
  DashboardSession,
  DashboardSessionStore,
  StoredDashboardSession,
} from "./dashboardSessionTypes.js";

export const DEFAULT_DASHBOARD_SESSION_LIFETIME_MS = 30 * 24 * 60 * 60 * 1000;

type RefreshedSessionTokens = Pick<DashboardSession, "accessToken" | "refreshToken" | "accessExpiresAt">;

export interface CreateDashboardSessionEngineOptions {
  secret: string;
  store: DashboardSessionStore;
  refreshDiscordToken(refreshToken: string): Promise<DashboardOAuthTokenResult>;
  sessionLifetimeMs?: number;
}

export function createDashboardSessionEngine(options: CreateDashboardSessionEngineOptions) {
  const encryptionKey = deriveDashboardSessionKey(options.secret);
  const lifetimeMs = options.sessionLifetimeMs ?? DEFAULT_DASHBOARD_SESSION_LIFETIME_MS;
  const refreshFlights = new Map<string, Promise<RefreshedSessionTokens | null>>();

  async function refreshSessionTokens(
    doc: StoredDashboardSession,
    refreshToken: string,
  ): Promise<RefreshedSessionTokens | null> {
    const refreshed = await options.refreshDiscordToken(refreshToken);
    if (!refreshed.ok || !refreshed.accessToken) {
      await options.store.deleteById(doc.id);
      return null;
    }

    const nextRefreshToken = dashboardNextRefreshToken(refreshToken, refreshed.refreshToken);
    const nextAccessExpiresAt = dashboardAccessExpiresAt(refreshed.expiresIn);
    const updated = await options.store.updateTokens(doc.id, {
      encryptedAccessToken: encryptDashboardSessionText(refreshed.accessToken, encryptionKey),
      encryptedRefreshToken: nextRefreshToken ? encryptDashboardSessionText(nextRefreshToken, encryptionKey) : null,
      accessExpiresAt: nextAccessExpiresAt ? new Date(nextAccessExpiresAt) : null,
      updatedAt: new Date(),
    });
    if (!updated) return null;
    return {
      accessToken: refreshed.accessToken,
      refreshToken: nextRefreshToken,
      accessExpiresAt: nextAccessExpiresAt,
    };
  }

  async function createSession(tokens: DashboardOAuthTokenResult, secure: boolean) {
    if (!tokens.ok || !tokens.accessToken) throw new Error(tokens.error || "missing_access_token");
    const rawSessionId = createDashboardSessionId();
    const now = Date.now();
    const expiresAt = now + lifetimeMs;
    const accessExpiresAt = dashboardAccessExpiresAt(tokens.expiresIn, now);
    await options.store.insert({
      sessionHash: dashboardSessionHash(rawSessionId),
      encryptedAccessToken: encryptDashboardSessionText(tokens.accessToken, encryptionKey),
      encryptedRefreshToken: tokens.refreshToken ? encryptDashboardSessionText(tokens.refreshToken, encryptionKey) : null,
      accessExpiresAt: accessExpiresAt ? new Date(accessExpiresAt) : null,
      expiresAt: new Date(expiresAt),
      createdAt: new Date(now),
      updatedAt: new Date(now),
    });
    return {
      session: {
        id: rawSessionId,
        accessToken: tokens.accessToken,
        refreshToken: tokens.refreshToken ?? null,
        accessExpiresAt,
        expiresAt,
      },
      setCookie: serializeDashboardCookie(DASHBOARD_SESSION_COOKIE, rawSessionId, {
        maxAgeSeconds: Math.floor(lifetimeMs / 1000),
        secure,
        sameSite: "Lax",
      }),
    };
  }

  async function getSession(cookieHeader: string | undefined): Promise<DashboardSession | null> {
    const rawSessionId = dashboardCookieValue(cookieHeader, DASHBOARD_SESSION_COOKIE);
    if (!rawSessionId) return null;
    const hashedSessionId = dashboardSessionHash(rawSessionId);
    const doc = await options.store.findByHash(hashedSessionId);
    if (!doc || dashboardSessionExpired(doc.expiresAt)) {
      if (doc) await options.store.deleteById(doc.id);
      return null;
    }

    let accessToken: string;
    let refreshToken: string | null;
    try {
      accessToken = decryptDashboardSessionText(doc.encryptedAccessToken, encryptionKey);
      refreshToken = doc.encryptedRefreshToken ? decryptDashboardSessionText(doc.encryptedRefreshToken, encryptionKey) : null;
    } catch {
      await options.store.deleteById(doc.id);
      return null;
    }

    let accessExpiresAt = doc.accessExpiresAt?.getTime() ?? null;
    if (dashboardSessionNeedsRefresh(accessExpiresAt)) {
      if (!refreshToken) {
        await options.store.deleteById(doc.id);
        return null;
      }
      const refreshTokenForFlight = refreshToken;
      const refreshed = await runSingleFlight(
        refreshFlights,
        hashedSessionId,
        () => refreshSessionTokens(doc, refreshTokenForFlight),
      );
      if (!refreshed) return null;
      accessToken = refreshed.accessToken;
      refreshToken = refreshed.refreshToken;
      accessExpiresAt = refreshed.accessExpiresAt;
    }

    return {
      id: rawSessionId,
      accessToken,
      refreshToken,
      accessExpiresAt,
      expiresAt: doc.expiresAt.getTime(),
    };
  }

  async function destroySession(cookieHeader: string | undefined) {
    const rawSessionId = dashboardCookieValue(cookieHeader, DASHBOARD_SESSION_COOKIE);
    if (!rawSessionId) return;
    await options.store.deleteByHash(dashboardSessionHash(rawSessionId));
  }

  return { createSession, getSession, destroySession };
}
