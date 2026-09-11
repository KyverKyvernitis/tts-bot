import {
  DASHBOARD_OAUTH_COOKIE,
  DASHBOARD_SESSION_COOKIE,
  issueDashboardOAuthState,
  serializeDashboardCookie,
  validateDashboardOAuthState,
} from "./dashboardSessionPrimitives.js";
import {
  createDashboardSessionEngine,
  DEFAULT_DASHBOARD_SESSION_LIFETIME_MS,
} from "./dashboardSessionEngine.js";
import { createMongoDashboardSessionStore } from "./dashboardSessionStore.js";
import type { DashboardOAuthTokenResult, DashboardSession } from "./dashboardSessionTypes.js";

export type { DashboardOAuthTokenResult, DashboardSession } from "./dashboardSessionTypes.js";

interface CreateDashboardSessionServiceOptions {
  mongoUri: string;
  mongoDbName: string;
  mongoCollectionName?: string;
  secret: string;
  refreshDiscordToken(refreshToken: string): Promise<DashboardOAuthTokenResult>;
  sessionLifetimeMs?: number;
}

export interface DashboardSessionService {
  readonly sessionCookieName: string;
  readonly oauthCookieName: string;
  issueOAuthState(returnTo: unknown, secure: boolean): { state: string; setCookie: string };
  validateOAuthState(state: unknown, cookieHeader: string | undefined): { ok: true; returnTo: string } | { ok: false; reason: string };
  clearOAuthCookie(secure: boolean): string;
  createSession(tokens: DashboardOAuthTokenResult, secure: boolean): Promise<{ session: DashboardSession; setCookie: string }>;
  getSession(cookieHeader: string | undefined): Promise<DashboardSession | null>;
  destroySession(cookieHeader: string | undefined): Promise<void>;
  clearSessionCookie(secure: boolean): string;
}

export function createDashboardSessionService(options: CreateDashboardSessionServiceOptions): DashboardSessionService {
  if (!options.secret || options.secret.length < 24) {
    throw new Error("dashboard_session_secret_too_short");
  }

  const store = createMongoDashboardSessionStore({
    mongoUri: options.mongoUri,
    mongoDbName: options.mongoDbName,
    mongoCollectionName: options.mongoCollectionName,
  });
  const engine = createDashboardSessionEngine({
    secret: options.secret,
    store,
    refreshDiscordToken: options.refreshDiscordToken,
    sessionLifetimeMs: options.sessionLifetimeMs ?? DEFAULT_DASHBOARD_SESSION_LIFETIME_MS,
  });

  return {
    sessionCookieName: DASHBOARD_SESSION_COOKIE,
    oauthCookieName: DASHBOARD_OAUTH_COOKIE,
    issueOAuthState: (returnTo, secure) => issueDashboardOAuthState(options.secret, returnTo, secure),
    validateOAuthState: (state, cookieHeader) => validateDashboardOAuthState(options.secret, state, cookieHeader),
    clearOAuthCookie: (secure) => serializeDashboardCookie(DASHBOARD_OAUTH_COOKIE, "", { maxAgeSeconds: 0, secure, sameSite: "Lax" }),
    createSession: engine.createSession,
    getSession: engine.getSession,
    destroySession: engine.destroySession,
    clearSessionCookie: (secure) => serializeDashboardCookie(DASHBOARD_SESSION_COOKIE, "", { maxAgeSeconds: 0, secure, sameSite: "Lax" }),
  };
}
