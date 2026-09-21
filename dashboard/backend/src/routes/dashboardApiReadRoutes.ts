import { buildDashboardCommands } from "../services/dashboardCommandsService.js";
import { createDashboardFullLoader } from "../services/dashboardFullLoader.js";
import { fetchDiscordAttachmentPreview } from "../services/dashboardMediaPreviewService.js";
import { loadDashboardEdgeVoices } from "../services/dashboardEdgeVoiceCatalog.js";
import {
  createDashboardInviteUrl,
  getDiscordBotIdentity,
  listDashboardServers,
  listGuildChannelsAndRoles,
  verifyDashboardInviteAccess,
} from "../services/discordAuthService.js";
import { requireDashboardAccess, requireSession } from "./dashboardAuthRoutes.js";
import { dashboardGuildId, firstString, sendNoStoreJson, takeRateLimit } from "./dashboardRouteUtils.js";
import type { RegisterDashboardApiRoutesOptions } from "./dashboardApiRouteTypes.js";

export function registerDashboardApiReadRoutes({
  app,
  configService,
  sessionService,
}: RegisterDashboardApiRoutesOptions) {
  const loadFullDashboard = createDashboardFullLoader({
    configService,
    listGuildOptions: listGuildChannelsAndRoles,
    getBotIdentity: getDiscordBotIdentity,
  });

  app.get("/api/dashboard/tts/voices", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    if (!takeRateLimit(req, "tts-voices", 60, 60_000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, ...await loadDashboardEdgeVoices(firstString(req.query.refresh) === "1") });
  });

  app.get("/api/dashboard/servers", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    const result = await listDashboardServers(session.accessToken, session.user);
    sendNoStoreJson(res, result.status, result.ok
      ? { ok: true, user: result.user, manageable: result.manageable, needsInvite: result.needsInvite }
      : { ok: false, user: result.user, manageable: [], needsInvite: [], error: result.error || "servers_failed" });
  });

  app.get("/api/dashboard/guild/:guildId/invite", async (req, res) => {
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    const guildId = dashboardGuildId(req);
    const access = await verifyDashboardInviteAccess(session.accessToken, guildId);
    if (!access.ok) {
      sendNoStoreJson(res, access.status, { ok: false, error: access.reason });
      return;
    }
    const inviteUrl = createDashboardInviteUrl(guildId);
    if (!inviteUrl) {
      sendNoStoreJson(res, 500, { ok: false, error: "invite_not_configured" });
      return;
    }
    sendNoStoreJson(res, 200, { ok: true, guild_id: guildId, invite_url: inviteUrl });
  });

  app.get("/api/dashboard/bootstrap", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    const bot = await getDiscordBotIdentity().catch(() => null);
    sendNoStoreJson(res, 200, {
      ok: true,
      user: auth.user,
      bot,
      guild_id: auth.guildId,
      sections: configService.listSections().map(({ id, label, emoji, description }) => ({ id, label, emoji, description })),
    });
  });

  app.get("/api/dashboard/guild/:guildId/full", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, await loadFullDashboard(auth));
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "dashboard_load_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/full-progress", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;

    res.status(200);
    res.setHeader("Cache-Control", "no-store, max-age=0, no-transform");
    res.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();

    const total = 5;
    let completed = 1;
    const writeEvent = (event: unknown) => {
      if (res.destroyed || res.writableEnded) return;
      res.write(`${JSON.stringify(event)}\n`);
    };

    writeEvent({ type: "progress", completed, total, step: "access" });
    try {
      const payload = await loadFullDashboard(auth, (step) => {
        completed += 1;
        writeEvent({ type: "progress", completed, total, step });
      });
      writeEvent({ type: "result", payload });
    } catch (error) {
      writeEvent({ type: "error", status: 500, error: error instanceof Error ? error.message : "dashboard_load_failed" });
    } finally {
      if (!res.destroyed && !res.writableEnded) res.end();
    }
  });

  app.get("/api/dashboard/guild/:guildId/summary", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, { ok: true, ...await configService.getSummary(auth.guildId) });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "summary_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/settings", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      sendNoStoreJson(res, 200, { ok: true, ...await configService.getSettings(auth.guildId) });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "settings_failed" });
    }
  });

  app.get("/api/dashboard/guild/:guildId/options", async (req, res) => {
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    const result = await listGuildChannelsAndRoles(auth.guildId);
    sendNoStoreJson(res, result.ok ? 200 : 502, {
      ok: result.ok,
      guildId: auth.guildId,
      channels: result.channels,
      roles: result.roles,
      error: result.error ?? null,
    });
  });

  app.get("/api/dashboard/guild/:guildId/commands", async (req, res) => {
    if (!takeRateLimit(req, "commands-read", 180, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const auth = await requireDashboardAccess(req, res, sessionService);
    if (!auth) return;
    try {
      const context = await configService.getCommandContext(auth.guildId);
      sendNoStoreJson(res, 200, { ok: true, guildId: auth.guildId, ...buildDashboardCommands(context) });
    } catch (error) {
      sendNoStoreJson(res, 500, { ok: false, error: error instanceof Error ? error.message : "commands_failed" });
    }
  });

  app.get("/api/dashboard/media-preview", async (req, res) => {
    if (!takeRateLimit(req, "media-preview", 120, 10 * 60 * 1000)) {
      sendNoStoreJson(res, 429, { ok: false, error: "rate_limited" });
      return;
    }
    const session = await requireSession(req, res, sessionService);
    if (!session) return;
    const result = await fetchDiscordAttachmentPreview(firstString(req.query.url));
    if (!result.ok) {
      sendNoStoreJson(res, result.status, { ok: false, error: result.error });
      return;
    }
    res.setHeader("Cache-Control", "private, max-age=300, stale-while-revalidate=300");
    res.setHeader("Content-Type", result.contentType);
    res.setHeader("Content-Length", String(result.body.byteLength));
    res.status(200).end(result.body);
  });
}
