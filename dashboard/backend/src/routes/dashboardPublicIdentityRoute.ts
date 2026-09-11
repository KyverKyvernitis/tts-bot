import type { Express } from "express";
import { getDiscordBotIdentity, getDiscordSupportServerIdentity } from "../services/discordAuthService.js";

export function registerDashboardPublicIdentityRoute(app: Express) {
  app.get("/api/public/identity", async (_req, res) => {
    const [bot, supportServer] = await Promise.all([
      getDiscordBotIdentity().catch(() => null),
      getDiscordSupportServerIdentity().catch(() => null),
    ]);
    res.setHeader("Cache-Control", "public, max-age=300, stale-while-revalidate=900");
    res.type("application/json");
    res.status(200).json({ ok: true, bot, supportServer });
  });
}
