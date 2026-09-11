import type { Express } from "express";
import type { DashboardOAuthTokenResult, DashboardSessionService } from "../services/dashboardSessionService.js";
import { registerDashboardHealthRoute } from "./dashboardHealthRoute.js";
import { registerDashboardOAuthRoutes } from "./dashboardOAuthRoutes.js";
import { registerDashboardPublicIdentityRoute } from "./dashboardPublicIdentityRoute.js";
import { registerDashboardSessionRoutes } from "./dashboardSessionRoutes.js";

export type { GuildAuth, SessionAuth } from "./dashboardAuthGuard.js";
export { requireDashboardAccess, requireSession } from "./dashboardAuthGuard.js";

export interface RegisterDashboardAuthRoutesOptions {
  app: Express;
  sessionService: DashboardSessionService;
  discordClientId: string;
  publicOrigin: string;
  allowedOrigins: Set<string>;
  exchangeDiscordCode(code: string, redirectUri: string): Promise<DashboardOAuthTokenResult>;
}

export function registerDashboardAuthRoutes(options: RegisterDashboardAuthRoutesOptions) {
  registerDashboardHealthRoute(options.app);
  registerDashboardOAuthRoutes(options);
  registerDashboardPublicIdentityRoute(options.app);
  registerDashboardSessionRoutes(options);
}
