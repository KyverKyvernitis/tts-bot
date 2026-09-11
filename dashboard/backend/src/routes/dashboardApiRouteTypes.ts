import type { Express } from "express";
import type { DashboardConfigService } from "../services/dashboardConfigService.js";
import type { DashboardSessionService } from "../services/dashboardSessionService.js";

export interface RegisterDashboardApiRoutesOptions {
  app: Express;
  configService: DashboardConfigService;
  sessionService: DashboardSessionService;
  publicOrigin: string;
  allowedOrigins: Set<string>;
}
