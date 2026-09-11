import type { Express } from "express";
import { sendNoStoreJson } from "./dashboardRouteUtils.js";

export function registerDashboardHealthRoute(app: Express) {
  app.get(["/health", "/api/health"], (_req, res) => {
    sendNoStoreJson(res, 200, {
      ok: true,
      service: "osaka-dashboard",
      version: "2.0.0",
      runtime: "web",
      time: new Date().toISOString(),
    });
  });
}
