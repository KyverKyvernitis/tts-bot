import type { Express, Request, Response } from "express";
import type { DashboardConfigService } from "../services/dashboardConfigService.js";
import { createDashboardInviteUrl, getDiscordUserIdentity, listDashboardServers, verifyDashboardAccess } from "../services/discordAuthService.js";

export interface RegisterDashboardRoutesOptions {
  app: Express;
  configService: DashboardConfigService;
  exchangeDiscordCode(code: string, redirectUri?: string): Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }>;
}

function sendNoStoreJson(res: Response, status: number, payload: unknown) {
  res.setHeader("Cache-Control", "no-store");
  res.type("application/json");
  res.status(status).json(payload);
}

function bearer(req: Request): string {
  const header = req.headers.authorization;
  if (typeof header !== "string") return "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  return match ? match[1].trim() : "";
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "bigint") return String(value).trim();
  }
  return "";
}

function dashboardGuildId(req: Request): string {
  const body = req.body && typeof req.body === "object" ? req.body as Record<string, unknown> : {};
  return firstString(req.params.guildId, req.query.guild_id, body.guild_id, body.guildId);
}

async function requireDashboardAccess(req: Request, res: Response): Promise<{ ok: true; userId: string; guildId: string; user: { id: string; username?: string | null; global_name?: string | null; avatar?: string | null } } | null> {
  const guildId = dashboardGuildId(req);
  const access = await verifyDashboardAccess(bearer(req), guildId);
  if (!access.ok || !access.user) {
    sendNoStoreJson(res, access.status, { ok: false, error: access.reason || "access_denied", detail: access.detail ?? null });
    return null;
  }
  return { ok: true, userId: access.user.id, guildId, user: access.user };
}

export function registerDashboardRoutes({ app, configService, exchangeDiscordCode }: RegisterDashboardRoutesOptions) {
  const healthHandler = async (_req: Request, res: Response) => {
    sendNoStoreJson(res, 200, {
      ok: true,
      service: "dashboard",
      version: "1.1.0",
      legacy_sinuca: false,
      time: new Date().toISOString(),
    });
  };

  const sendBootstrap = async (req: Request, res: Response) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    sendNoStoreJson(res, 200, {
      ok: true,
      user: {
        id: auth.user.id,
        username: auth.user.username ?? null,
        global_name: auth.user.global_name ?? null,
        avatar: auth.user.avatar ?? null,
      },
      guild_id: auth.guildId,
      sections: configService.listSections().map(({ id, label, emoji, description }) => ({ id, label, emoji, description })),
    });
  };

  const sendSummary = async (req: Request, res: Response) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    try {
      const summary = await configService.getSummary(auth.guildId);
      sendNoStoreJson(res, 200, { ok: true, ...summary });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "summary_failed" });
    }
  };

  const sendSettings = async (req: Request, res: Response) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    try {
      const settings = await configService.getSettings(auth.guildId);
      sendNoStoreJson(res, 200, { ok: true, ...settings });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "settings_failed" });
    }
  };

  const saveSettings = async (req: Request, res: Response) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    try {
      const updates = (req.body && typeof req.body.updates === "object" && !Array.isArray(req.body.updates))
        ? req.body.updates as Record<string, unknown>
        : {};
      const result = await configService.updateSettings(auth.guildId, updates);
      sendNoStoreJson(res, 200, result);
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "save_failed" });
    }
  };


  const sessionHandler = async (req: Request, res: Response) => {
    const session = await getDiscordUserIdentity(bearer(req));
    if (!session.ok || !session.user) {
      sendNoStoreJson(res, session.status || 401, { ok: false, authenticated: false, error: "session_invalid" });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, authenticated: true, user: session.user });
  };

  const serversHandler = async (req: Request, res: Response) => {
    const result = await listDashboardServers(bearer(req));
    sendNoStoreJson(res, result.status, result.ok
      ? { ok: true, user: result.user, manageable: result.manageable, needsInvite: result.needsInvite }
      : { ok: false, user: result.user, manageable: [], needsInvite: [], error: result.error || "servers_failed" });
  };

  const inviteHandler = async (req: Request, res: Response) => {
    const guildId = dashboardGuildId(req);
    const session = await getDiscordUserIdentity(bearer(req));
    if (!session.ok || !session.user) {
      sendNoStoreJson(res, session.status || 401, { ok: false, error: "session_invalid" });
      return;
    }
    const inviteUrl = createDashboardInviteUrl(guildId);
    if (!inviteUrl) {
      sendNoStoreJson(res, 500, { ok: false, error: "invite_not_configured" });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, guild_id: guildId, invite_url: inviteUrl });
  };

  const dashboardRpcHandler = async (req: Request, res: Response): Promise<boolean> => {
    const body = req.body && typeof req.body === "object" ? req.body as Record<string, unknown> : {};
    const action = firstString(body.dashboard_action, body.action);
    if (!action) return false;

    if (action === "bootstrap") {
      await sendBootstrap(req, res);
      return true;
    }
    if (action === "summary") {
      await sendSummary(req, res);
      return true;
    }
    if (action === "settings") {
      await sendSettings(req, res);
      return true;
    }
    if (action === "settings:update" || action === "save_settings") {
      await saveSettings(req, res);
      return true;
    }

    sendNoStoreJson(res, 400, { ok: false, error: "unknown_dashboard_action", detail: action });
    return true;
  };

  const tokenHandler = async (req: Request, res: Response) => {
    if (await dashboardRpcHandler(req, res)) return;

    const code = String((req.body && req.body.code) || "").trim();
    const redirectUri = String((req.body && req.body.redirect_uri) || "").trim() || undefined;
    const result = await exchangeDiscordCode(code, redirectUri);
    if (!result.ok || !result.accessToken) {
      sendNoStoreJson(res, 400, { ok: false, error: result.error || "token_exchange_failed", detail: result.detail || null });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, access_token: result.accessToken });
  };

  app.get(["/health", "/api/health"], healthHandler);
  app.post(["/token", "/api/token"], tokenHandler);
  app.all(["/token", "/api/token"], (_req, res) => {
    sendNoStoreJson(res, 405, { ok: false, error: "method_not_allowed", detail: "Use POST com o código OAuth." });
  });

  app.get("/api/session", sessionHandler);
  app.get("/api/dashboard/servers", serversHandler);
  app.get("/api/dashboard/guild/:guildId/invite", inviteHandler);

  app.get("/api/dashboard/bootstrap", sendBootstrap);
  app.get("/api/dashboard/guild/:guildId/summary", sendSummary);
  app.get("/api/dashboard/guild/:guildId/settings", sendSettings);
  app.patch("/api/dashboard/guild/:guildId/settings", saveSettings);

  app.use("/api", (req, res) => {
    sendNoStoreJson(res, 404, { ok: false, error: "api_route_not_found", detail: `${req.method} ${req.originalUrl}` });
  });
}
