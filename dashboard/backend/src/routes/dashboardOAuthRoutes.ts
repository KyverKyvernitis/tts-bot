import type { Express } from "express";
import type { DashboardOAuthTokenResult, DashboardSessionService } from "../services/dashboardSessionService.js";
import {
  appendCookie,
  authErrorRedirect,
  callbackUrl,
  firstString,
  isSecureRequest,
  sendNoStoreJson,
  takeRateLimit,
} from "./dashboardRouteUtils.js";
import { buildDiscordOAuthAuthorizeUrl } from "./dashboardOAuthModel.js";

export interface RegisterDashboardOAuthRoutesOptions {
  app: Express;
  sessionService: DashboardSessionService;
  discordClientId: string;
  publicOrigin: string;
  exchangeDiscordCode(code: string, redirectUri: string): Promise<DashboardOAuthTokenResult>;
}

export function registerDashboardOAuthRoutes({
  app,
  sessionService,
  discordClientId,
  exchangeDiscordCode,
  publicOrigin,
}: RegisterDashboardOAuthRoutesOptions) {
  app.get("/api/auth/login", (req, res) => {
    if (!takeRateLimit(req, "auth-login", 20, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const redirectUri = callbackUrl(req, publicOrigin);
    if (!discordClientId || !redirectUri) {
      sendNoStoreJson(res, 503, { ok: false, error: "oauth_not_configured" });
      return;
    }
    const issued = sessionService.issueOAuthState(req.query.return_to, isSecureRequest(req));
    appendCookie(res, issued.setCookie);
    res.setHeader("Cache-Control", "no-store");
    res.redirect(302, buildDiscordOAuthAuthorizeUrl(discordClientId, redirectUri, issued.state));
  });

  app.get("/api/auth/callback", async (req, res) => {
    if (!takeRateLimit(req, "auth-callback", 30, 10 * 60 * 1000)) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, "rate_limited"));
      return;
    }
    const secure = isSecureRequest(req);
    appendCookie(res, sessionService.clearOAuthCookie(secure));
    const state = sessionService.validateOAuthState(req.query.state, req.headers.cookie);
    if (!state.ok) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, state.reason));
      return;
    }
    const oauthError = firstString(req.query.error);
    if (oauthError) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, oauthError));
      return;
    }
    const code = firstString(req.query.code);
    const redirectUri = callbackUrl(req, publicOrigin);
    const exchanged = await exchangeDiscordCode(code, redirectUri);
    if (!exchanged.ok || !exchanged.accessToken) {
      res.redirect(302, authErrorRedirect(req, publicOrigin, exchanged.error || "oauth_exchange_failed"));
      return;
    }
    try {
      const created = await sessionService.createSession(exchanged, secure);
      appendCookie(res, created.setCookie);
      res.setHeader("Cache-Control", "no-store");
      res.redirect(302, state.returnTo);
    } catch (error) {
      console.error("[dashboard-session] falha ao criar sessão", error instanceof Error ? error.message : String(error));
      res.redirect(302, authErrorRedirect(req, publicOrigin, "session_create_failed"));
    }
  });
}
