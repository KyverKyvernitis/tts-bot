import express from "express";
import { createServer } from "http";
import { registerDashboardRoutes } from "./routes/registerDashboardRoutes.js";
import { createDashboardConfigService } from "./services/dashboardConfigService.js";

const app = express();

app.use((req, res, next) => {
  const origin = typeof req.headers.origin === "string" ? req.headers.origin : "*";
  res.setHeader("Access-Control-Allow-Origin", origin);
  res.setHeader("Vary", "Origin");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With");
  res.setHeader("Access-Control-Allow-Credentials", "true");

  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }

  next();
});

app.use(express.json({ limit: "1mb" }));
app.use(express.urlencoded({ extended: false }));
app.use((req, _res, next) => {
  console.log("[activity-dashboard-http]", JSON.stringify({
    method: req.method,
    url: req.url ?? null,
    origin: req.headers.origin ?? null,
    referer: req.headers.referer ?? null,
    ua: req.headers["user-agent"] ?? null,
  }));
  next();
});

const discordClientId = process.env.VITE_DISCORD_CLIENT_ID || process.env.DISCORD_CLIENT_ID || "";
const discordClientSecret = process.env.DISCORD_CLIENT_SECRET || process.env.CLIENT_SECRET || "";

type OAuthTokenResult = {
  ok: boolean;
  accessToken: string | null;
  refreshToken: string | null;
  expiresIn: number | null;
  error: string | null;
  detail: string | null;
};

async function requestDiscordOAuthToken(params: URLSearchParams, logType: string): Promise<OAuthTokenResult> {
  console.log("[activity-dashboard-oauth] token request", JSON.stringify({
    type: logType,
    hasCode: Boolean(params.get("code")),
    hasRefreshToken: Boolean(params.get("refresh_token")),
    hasClientId: Boolean(discordClientId),
    hasClientSecret: Boolean(discordClientSecret),
    hasRedirectUri: Boolean(params.get("redirect_uri")),
  }));

  if (!discordClientId || !discordClientSecret) {
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "oauth_not_configured", detail: null };
  }

  try {
    params.set("client_id", discordClientId);
    params.set("client_secret", discordClientSecret);

    const response = await fetch("https://discord.com/api/v10/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params,
    });

    const raw = await response.text();
    console.log("[activity-dashboard-oauth] token response", JSON.stringify({ status: response.status, body: raw.slice(0, 240) || "empty" }));

    let data: { access_token?: string; refresh_token?: string; expires_in?: number; error?: string; error_description?: string } = {};
    try {
      data = JSON.parse(raw) as { access_token?: string; refresh_token?: string; expires_in?: number; error?: string; error_description?: string };
    } catch {
      return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "token_invalid_json", detail: raw.slice(0, 240) || null };
    }

    if (!response.ok || !data.access_token) {
      console.error("[activity-dashboard-oauth] token exchange failed", response.status, data);
      return {
        ok: false,
        accessToken: null,
        refreshToken: null,
        expiresIn: null,
        error: data.error ?? "token_exchange_failed",
        detail: data.error_description ?? null,
      };
    }

    const expiresIn = typeof data.expires_in === "number" && Number.isFinite(data.expires_in) ? data.expires_in : null;
    return {
      ok: true,
      accessToken: data.access_token,
      refreshToken: typeof data.refresh_token === "string" && data.refresh_token.trim() ? data.refresh_token : null,
      expiresIn,
      error: null,
      detail: null,
    };
  } catch (error) {
    console.error("[activity-dashboard-oauth] token exchange error", error);
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "token_exchange_exception", detail: null };
  }
}

async function exchangeDiscordCode(code: string, redirectUri?: string): Promise<OAuthTokenResult> {
  if (!code) {
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "missing_code", detail: null };
  }
  const params = new URLSearchParams();
  params.set("grant_type", "authorization_code");
  params.set("code", code);
  if (redirectUri) params.set("redirect_uri", redirectUri);
  return await requestDiscordOAuthToken(params, "authorization_code");
}

async function refreshDiscordToken(refreshToken: string): Promise<OAuthTokenResult> {
  if (!refreshToken) {
    return { ok: false, accessToken: null, refreshToken: null, expiresIn: null, error: "missing_refresh_token", detail: null };
  }
  const params = new URLSearchParams();
  params.set("grant_type", "refresh_token");
  params.set("refresh_token", refreshToken);
  return await requestDiscordOAuthToken(params, "refresh_token");
}

const dashboardConfigService = createDashboardConfigService({
  mongoUri: process.env.MONGODB_URI || process.env.MONGO_URI || "",
  mongoDbName: process.env.MONGODB_DB || process.env.MONGO_DB_NAME || process.env.MONGODB_DB_NAME || "chat_revive",
  mongoCollectionName: process.env.MONGODB_COLLECTION || process.env.MONGO_COLLECTION_NAME || process.env.MONGODB_COLLECTION_NAME || "settings",
});

registerDashboardRoutes({
  app,
  configService: dashboardConfigService,
  exchangeDiscordCode,
  refreshDiscordToken,
});

const server = createServer(app);
const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[activity-dashboard-server] ouvindo na porta ${port}`);
});
