import type { Express } from "express";
import type { DashboardConfigService } from "../services/dashboardConfigService.js";
import type { DashboardOAuthTokenResult, DashboardSessionService } from "../services/dashboardSessionService.js";
import { registerDashboardApiRoutes } from "./dashboardApiRoutes.js";
import { registerDashboardAuthRoutes } from "./dashboardAuthRoutes.js";
import { sendNoStoreJson } from "./dashboardRouteUtils.js";

export interface RegisterDashboardRoutesOptions {
  app: Express;
  configService: DashboardConfigService;
  sessionService: DashboardSessionService;
  discordClientId: string;
  publicOrigin: string;
  allowedOrigins: Set<string>;
  exchangeDiscordCode(code: string, redirectUri: string): Promise<DashboardOAuthTokenResult>;
}

export function registerDashboardRoutes(options: RegisterDashboardRoutesOptions) {
  const { app } = options;
  registerDashboardAuthRoutes(options);
  registerDashboardApiRoutes(options);
  app.all(["/token", "/api/token", "/session", "/api/session"], (_req, res) => {
    sendNoStoreJson(res, 410, { ok: false, error: "legacy_api_removed" });
  });

  app.use("/api", (req, res) => {
    sendNoStoreJson(res, 404, { ok: false, error: "api_route_not_found", detail: `${req.method} ${req.path}` });
  });
}

