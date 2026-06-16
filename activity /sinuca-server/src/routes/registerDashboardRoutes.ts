import type { Express, Request, Response } from "express";
import type { DashboardConfigService } from "../services/dashboardConfigService.js";
import { verifyDashboardAccess } from "../services/discordAuthService.js";

export interface RegisterDashboardRoutesOptions {
  app: Express;
  configService: DashboardConfigService;
  exchangeDiscordCode(code: string): Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }>;
}

function sendNoStoreJson(res: Response, status: number, payload: unknown) {
  res.setHeader("Cache-Control", "no-store");
  res.status(status).json(payload);
}

function bearer(req: Request): string {
  const header = req.headers.authorization;
  if (typeof header !== "string") return "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  return match ? match[1].trim() : "";
}

async function requireDashboardAccess(req: Request, res: Response): Promise<{ ok: true; userId: string; guildId: string } | null> {
  const guildId = String(req.params.guildId || req.query.guild_id || "").trim();
  const access = await verifyDashboardAccess(bearer(req), guildId);
  if (!access.ok || !access.user) {
    sendNoStoreJson(res, access.status, { ok: false, error: access.reason || "access_denied", detail: access.detail ?? null });
    return null;
  }
  return { ok: true, userId: access.user.id, guildId };
}

export function registerDashboardRoutes({ app, configService, exchangeDiscordCode }: RegisterDashboardRoutesOptions) {
  app.get("/health", async (_req, res) => {
    sendNoStoreJson(res, 200, {
      ok: true,
      service: "activity-dashboard",
      version: "1.0.0",
      legacy_sinuca: false,
      time: new Date().toISOString(),
    });
  });

  app.post("/token", async (req, res) => {
    const code = String((req.body && req.body.code) || "").trim();
    const result = await exchangeDiscordCode(code);
    if (!result.ok || !result.accessToken) {
      sendNoStoreJson(res, 400, { ok: false, error: result.error || "token_exchange_failed", detail: result.detail || null });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, access_token: result.accessToken });
  });

  app.get("/api/dashboard/bootstrap", async (req, res) => {
    const guildId = String(req.query.guild_id || "").trim();
    const access = await verifyDashboardAccess(bearer(req), guildId);
    if (!access.ok || !access.user) {
      sendNoStoreJson(res, access.status, { ok: false, error: access.reason || "access_denied" });
      return;
    }
    sendNoStoreJson(res, 200, {
      ok: true,
      user: {
        id: access.user.id,
        username: access.user.username ?? null,
        global_name: access.user.global_name ?? null,
        avatar: access.user.avatar ?? null,
      },
      guild_id: guildId,
      sections: configService.listSections().map(({ id, label, emoji, description }) => ({ id, label, emoji, description })),
    });
  });

  app.get("/api/dashboard/guild/:guildId/summary", async (req, res) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    try {
      const summary = await configService.getSummary(auth.guildId);
      sendNoStoreJson(res, 200, { ok: true, ...summary });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "summary_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/settings", async (req, res) => {
    const auth = await requireDashboardAccess(req, res);
    if (!auth) return;
    try {
      const settings = await configService.getSettings(auth.guildId);
      sendNoStoreJson(res, 200, { ok: true, ...settings });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "settings_failed" });
    }
  });

  app.patch("/api/dashboard/guild/:guildId/settings", async (req, res) => {
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
  });
}
