import { DashboardConfigValidationError, DashboardConfigValueError } from "../services/dashboardConfigService.js";
import { requireDashboardAccess } from "./dashboardAuthRoutes.js";
import { mutationOriginAllowed, sendNoStoreJson, takeRateLimit } from "./dashboardRouteUtils.js";
import type { RegisterDashboardApiRoutesOptions } from "./dashboardApiRouteTypes.js";

export function registerDashboardApiMutationRoutes({
  app,
  configService,
  sessionService,
  publicOrigin,
  allowedOrigins,
}: RegisterDashboardApiRoutesOptions) {
  app.patch("/api/dashboard/guild/:guildId/settings", async (req, res) => {
    if (!mutationOriginAllowed(req, publicOrigin, allowedOrigins)) {
      sendNoStoreJson(res, 403, { ok: false, error: "origin_denied" });
      return;
    }
    if (!takeRateLimit(req, "settings-save", 120, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      const updates = req.body && typeof req.body.updates === "object" && !Array.isArray(req.body.updates)
        ? req.body.updates as Record<string, unknown>
        : {};
      const result = await configService.updateSettings(auth.guildId, updates);
      try {
        const summary = await configService.getSummary(auth.guildId);
        sendNoStoreJson(res, 200, { ...result, summary: summary.sections });
      } catch {
        sendNoStoreJson(res, 200, { ...result, summary: null, summary_error: "summary_refresh_failed" });
      }
    } catch (error) {
      if (error instanceof DashboardConfigValidationError) {
        sendNoStoreJson(res, 400, { ok: false, error: error.code, message: error.message, section: error.sectionId, issues: error.issues });
        return;
      }
      if (error instanceof DashboardConfigValueError) {
        sendNoStoreJson(res, 400, { ok: false, error: error.code, message: error.message, field: error.fieldId });
        return;
      }
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "save_failed" });
    }
  });
}
