import type { Request, Response } from "express";
import type { DashboardSessionService } from "../services/dashboardSessionService.js";
import { getDiscordUserIdentity, verifyDashboardAccess } from "../services/discordAuthService.js";
import { appendCookie, dashboardGuildId, isSecureRequest, sendNoStoreJson } from "./dashboardRouteUtils.js";

export type SessionAuth = {
  accessToken: string;
  user: {
    id: string;
    username?: string | null;
    global_name?: string | null;
    avatar?: string | null;
    avatarUrl?: string | null;
  };
};

export type GuildAuth = SessionAuth & { guildId: string };

export async function requireSession(
  req: Request,
  res: Response,
  sessionService: DashboardSessionService,
): Promise<SessionAuth | null> {
  let session;
  try {
    session = await sessionService.getSession(req.headers.cookie);
  } catch (error) {
    console.error("[dashboard-session] falha ao ler sessão", error instanceof Error ? error.message : String(error));
    sendNoStoreJson(res, 503, { ok: false, authenticated: false, error: "session_store_unavailable" });
    return null;
  }
  if (!session) {
    sendNoStoreJson(res, 401, { ok: false, authenticated: false, error: "session_required" });
    return null;
  }
  const identity = await getDiscordUserIdentity(session.accessToken);
  if (!identity.ok || !identity.user) {
    if (identity.status === 401 || identity.status === 403) {
      await sessionService.destroySession(req.headers.cookie).catch(() => undefined);
      appendCookie(res, sessionService.clearSessionCookie(isSecureRequest(req)));
      sendNoStoreJson(res, 401, { ok: false, authenticated: false, error: "session_invalid" });
    } else {
      sendNoStoreJson(res, 502, { ok: false, authenticated: false, error: "discord_unavailable" });
    }
    return null;
  }
  return { accessToken: session.accessToken, user: identity.user };
}

export async function requireDashboardAccess(
  req: Request,
  res: Response,
  sessionService: DashboardSessionService,
): Promise<GuildAuth | null> {
  const session = await requireSession(req, res, sessionService);
  if (!session) return null;
  const guildId = dashboardGuildId(req);
  const access = await verifyDashboardAccess(session.accessToken, guildId, session.user);
  if (!access.ok || !access.user) {
    sendNoStoreJson(res, access.status, { ok: false, error: access.reason || "access_denied", detail: access.detail ?? null });
    return null;
  }
  return { ...session, guildId, user: access.user };
}
