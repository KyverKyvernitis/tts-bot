import type { Express } from "express";
import type { DashboardSessionService } from "../services/dashboardSessionService.js";
import { requireSession } from "./dashboardAuthGuard.js";
import { appendCookie, isSecureRequest, mutationOriginAllowed, sendNoStoreJson } from "./dashboardRouteUtils.js";

export interface RegisterDashboardSessionRoutesOptions {
  app: Express;
  sessionService: DashboardSessionService;
  publicOrigin: string;
  allowedOrigins: Set<string>;
}

export function registerDashboardSessionRoutes({
  app,
  sessionService,
  publicOrigin,
  allowedOrigins,
}: RegisterDashboardSessionRoutesOptions) {
  app.get("/api/auth/session", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    sendNoStoreJson(res, 200, { ok: true, authenticated: true, user: session.user });
  });

  app.post("/api/auth/logout", async (req, res) => {
    if (!mutationOriginAllowed(req, publicOrigin, allowedOrigins)) {
      sendNoStoreJson(res, 403, { ok: false, error: "origin_denied" });
      return;
    }
    await sessionService.destroySession(req.headers.cookie).catch(() => undefined);
    appendCookie(res, sessionService.clearSessionCookie(isSecureRequest(req)));
    sendNoStoreJson(res, 200, { ok: true });
  });
}
