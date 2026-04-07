import express from "express";
import { createServer } from "http";
import { WebSocketServer } from "ws";
import { registerActivityRoutes } from "./routes/registerActivityRoutes.js";
import { createBalanceService } from "./services/balanceService.js";
import { createActivityRealtimeRuntime } from "./realtime/runtime.js";
import { registerSocketServer } from "./realtime/registerSocketServer.js";

const app = express();

app.use((req, res, next) => {
  const origin = typeof req.headers.origin === "string" ? req.headers.origin : "*";
  res.setHeader("Access-Control-Allow-Origin", origin);
  res.setHeader("Vary", "Origin");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With");
  res.setHeader("Access-Control-Allow-Credentials", "true");

  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }

  next();
});
app.use(express.json());
app.use(express.urlencoded({ extended: false }));
app.use((req, _res, next) => {
  console.log("[sinuca-http]", JSON.stringify({
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

async function exchangeDiscordCode(code: string): Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }> {
  console.log("[sinuca-oauth] token request", JSON.stringify({
    hasCode: Boolean(code),
    codeLength: code.length,
    hasClientId: Boolean(discordClientId),
    hasClientSecret: Boolean(discordClientSecret),
  }));

  if (!code) {
    return { ok: false, accessToken: null, error: "missing_code", detail: null };
  }
  if (!discordClientId || !discordClientSecret) {
    return { ok: false, accessToken: null, error: "oauth_not_configured", detail: null };
  }

  try {
    const params = new URLSearchParams();
    params.set("client_id", discordClientId);
    params.set("client_secret", discordClientSecret);
    params.set("grant_type", "authorization_code");
    params.set("code", code);

    const response = await fetch("https://discord.com/api/v10/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params,
    });

    const raw = await response.text();
    console.log("[sinuca-oauth] token response", JSON.stringify({ status: response.status, body: raw.slice(0, 240) || "empty" }));

    let data: { access_token?: string; error?: string; error_description?: string } = {};
    try {
      data = JSON.parse(raw) as { access_token?: string; error?: string; error_description?: string };
    } catch {
      return { ok: false, accessToken: null, error: "token_invalid_json", detail: raw.slice(0, 240) || null };
    }

    if (!response.ok || !data.access_token) {
      console.error("[sinuca-oauth] token exchange failed", response.status, data);
      return {
        ok: false,
        accessToken: null,
        error: data.error ?? "token_exchange_failed",
        detail: data.error_description ?? null,
      };
    }

    return { ok: true, accessToken: data.access_token, error: null, detail: null };
  } catch (error) {
    console.error("[sinuca-oauth] token exchange error", error);
    return { ok: false, accessToken: null, error: "token_exchange_exception", detail: null };
  }
}

const balanceService = createBalanceService({
  mongoUri: process.env.MONGODB_URI || process.env.MONGO_URI || "",
  mongoDbName: process.env.MONGODB_DB || process.env.MONGO_DB_NAME || process.env.MONGODB_DB_NAME || "chat_revive",
  mongoCollectionName: process.env.MONGODB_COLLECTION || process.env.MONGO_COLLECTION_NAME || process.env.MONGODB_COLLECTION_NAME || "settings",
});

const realtimeRuntime = createActivityRealtimeRuntime();

registerActivityRoutes({
  app,
  runtime: realtimeRuntime,
  balanceService,
  exchangeDiscordCode,
});

const server = createServer(app);
server.on("upgrade", (req) => {
  console.log("[sinuca-upgrade]", JSON.stringify({
    url: req.url ?? null,
    origin: req.headers.origin ?? null,
    referer: req.headers.referer ?? null,
    ua: req.headers["user-agent"] ?? null,
  }));
});

const wss = new WebSocketServer({ server, path: "/ws" });
const stopRealtimeLifecycle = realtimeRuntime.startLifecycle();
const stopSocketServer = registerSocketServer({
  wss,
  runtime: realtimeRuntime,
  balanceService,
  exchangeDiscordCode,
});

server.on("close", () => {
  stopSocketServer();
  stopRealtimeLifecycle();
});

const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[sinuca-server] ouvindo na porta ${port}`);
});
