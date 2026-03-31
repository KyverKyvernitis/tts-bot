import express, { type Request, type Response } from "express";
import { createServer } from "http";
import { MongoClient } from "mongodb";
import type { IncomingMessage } from "http";
import type { WebSocket } from "ws";
import { WebSocketServer } from "ws";
import {
  addPlayer,
  createRoom,
  getRoom,
  getSubscribers,
  listRooms,
  removePlayer,
  setPlayerReady,
  subscribeSocket,
  toSnapshot,
  unsubscribeSocket,
} from "./rooms.js";
import type {
  BalanceDebugSnapshot,
  BalanceSnapshot,
  ClientMessage,
  ListRoomsPayload,
  ServerMessage,
  SessionContextPayload,
} from "./messages.js";
import { getInitialRuleSet } from "./gameRules.js";

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

function handleHealth(req: Request, res: Response) {
  console.log("[sinuca-health]", JSON.stringify({ origin: req.headers.origin ?? null, ua: req.headers["user-agent"] ?? null, url: req.url ?? null }));
  res.json({ ok: true, rules: getInitialRuleSet() });
}

app.get("/health", handleHealth);
app.get("/api/health", handleHealth);

function handleSession(req: Request, res: Response) {
  console.log("[sinuca-proxy-session]", JSON.stringify({
    hasProxyPayload: Boolean(req.headers["x-discord-proxy-payload"]),
    origin: req.headers.origin ?? null,
    referer: req.headers.referer ?? null,
    url: req.url ?? null,
    ua: req.headers["user-agent"] ?? null,
  }));
  const session = resolveRequestSession(req);
  console.log("[sinuca-proxy-session]", JSON.stringify({
    userId: session.userId,
    displayName: session.displayName,
    guildId: session.guildId,
    channelId: session.channelId,
    instanceId: session.instanceId,
    sessionSource: session.sessionSource,
    proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
    origin: req.headers.origin ?? null,
    referer: req.headers.referer ?? null,
    ua: req.headers["user-agent"] ?? null,
  }));
  res.json({
    ...session,
    proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
    hasProxyPayload: Boolean(req.headers["x-discord-proxy-payload"]),
  });
}

app.get("/session", handleSession);
app.get("/api/session", handleSession);

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

function handleTokenRequest(req: Request, res: Response) {
  const bodyCode = typeof req.body?.code === "string" ? req.body.code : "";
  const queryCode = typeof req.query?.code === "string" ? req.query.code : "";
  const code = bodyCode || queryCode;
  const codeSource = bodyCode ? "body" : (queryCode ? "query" : "missing");
  console.log("[sinuca-token-route]", JSON.stringify({
    method: req.method,
    url: req.url ?? null,
    origin: req.headers.origin ?? null,
    referer: req.headers.referer ?? null,
    ua: req.headers["user-agent"] ?? null,
    codeSource,
    hasCode: Boolean(code),
    codePrefix: code ? code.slice(0, 12) : null,
  }));
  void exchangeDiscordCode(code).then((result) => {
    console.log("[sinuca-token-route-result]", JSON.stringify({ ok: result.ok, error: result.error, detail: result.detail }));
    if (!result.ok || !result.accessToken) {
      res.status(result.error === "missing_code" ? 400 : result.error === "oauth_not_configured" ? 500 : 502).json({ error: result.error, detail: result.detail });
      return;
    }
    res.json({ access_token: result.accessToken });
  }).catch((error) => {
    console.error("[sinuca-oauth] token route unhandled error", error);
    res.status(500).json({ error: "token_exchange_exception" });
  });
}

app.post("/token", handleTokenRequest);
app.post("/api/token", handleTokenRequest);
app.get("/token", handleTokenRequest);
app.get("/api/token", handleTokenRequest);

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
const contextWatchers = new Map<string, Set<WebSocket>>();
const socketContext = new Map<WebSocket, string>();
const socketSession = new Map<WebSocket, SessionContextPayload>();
const balanceWatchers = new Map<WebSocket, { guildId: string; userId: string; lastSent: string }>();

const mongoUri = process.env.MONGODB_URI || process.env.MONGO_URI || "";
const mongoDbName = process.env.MONGODB_DB || process.env.MONGO_DB_NAME || process.env.MONGODB_DB_NAME || "chat_revive";
const mongoCollectionName = process.env.MONGODB_COLLECTION || process.env.MONGO_COLLECTION_NAME || process.env.MONGODB_COLLECTION_NAME || "settings";
let mongoClient: MongoClient | null = null;

function send(ws: WebSocket, payload: ServerMessage) {
  ws.send(JSON.stringify(payload));
}

function contextKey(payload: ListRoomsPayload) {
  return `${payload.mode}:${payload.guildId ?? ""}:${payload.channelId ?? ""}`;
}

function watchContext(ws: WebSocket, payload: ListRoomsPayload) {
  const nextKey = contextKey(payload);
  const previous = socketContext.get(ws);
  if (previous && previous !== nextKey) {
    contextWatchers.get(previous)?.delete(ws);
  }
  const bucket = contextWatchers.get(nextKey) ?? new Set<WebSocket>();
  bucket.add(ws);
  contextWatchers.set(nextKey, bucket);
  socketContext.set(ws, nextKey);
}

function unwatchContext(ws: WebSocket) {
  const previous = socketContext.get(ws);
  if (!previous) return;
  contextWatchers.get(previous)?.delete(ws);
  socketContext.delete(ws);
}

function broadcastRoom(roomId: string) {
  const room = getRoom(roomId);
  if (!room) return;
  const payload: ServerMessage = { type: "room_state", payload: toSnapshot(room) };
  for (const client of getSubscribers(roomId)) {
    send(client, payload);
  }
}

function broadcastRoomList(payload: ListRoomsPayload) {
  const watchers = contextWatchers.get(contextKey(payload));
  if (!watchers || watchers.size === 0) return;
  const message: ServerMessage = { type: "room_list", payload: listRooms(payload).map(toSnapshot) };
  for (const client of watchers) {
    send(client, message);
  }
}

async function ensureMongo() {
  if (!mongoUri) return null;
  if (!mongoClient) {
    mongoClient = new MongoClient(mongoUri);
    await mongoClient.connect();
  }
  return mongoClient.db(mongoDbName).collection(mongoCollectionName);
}

function normalizeIntString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const raw = String(value).trim();
  return raw ? raw : null;
}

function parseQuerySession(urlValue: string | undefined | null): Partial<SessionContextPayload> {
  if (!urlValue) return {};
  try {
    const url = new URL(urlValue, "https://sinuca.local");
    return {
      userId: normalizeIntString(url.searchParams.get("user_id") ?? url.searchParams.get("userId")),
      displayName: normalizeIntString(url.searchParams.get("display_name") ?? url.searchParams.get("displayName")),
      guildId: normalizeIntString(url.searchParams.get("guild_id") ?? url.searchParams.get("guildId")),
      channelId: normalizeIntString(url.searchParams.get("channel_id") ?? url.searchParams.get("channelId")),
      instanceId: normalizeIntString(url.searchParams.get("instance_id") ?? url.searchParams.get("instanceId")),
    };
  } catch {
    return {};
  }
}

function mergeNullableSession(base: SessionContextPayload, incoming: Partial<SessionContextPayload>): SessionContextPayload {
  return {
    userId: incoming.userId ?? base.userId,
    displayName: incoming.displayName ?? base.displayName,
    guildId: incoming.guildId ?? base.guildId,
    channelId: incoming.channelId ?? base.channelId,
    instanceId: incoming.instanceId ?? base.instanceId,
  };
}

function resolveRequestSession(req: IncomingMessage): SessionContextPayload & { sessionSource: string } {
  const fromProxy = decodeProxyPayload(req);
  const fromPath = parseQuerySession(req.url ?? null);
  const fromReferer = parseQuerySession(typeof req.headers.referer === "string" ? req.headers.referer : null);
  const merged = mergeNullableSession(mergeNullableSession(fromProxy, fromPath), fromReferer);
  const sessionSource = fromProxy.userId || fromProxy.guildId || fromProxy.channelId || fromProxy.instanceId
    ? "proxy"
    : (fromPath.guildId || fromPath.channelId || fromPath.instanceId || fromReferer.guildId || fromReferer.channelId || fromReferer.instanceId)
      ? "url_hint"
      : "empty";
  return { ...merged, sessionSource };
}

function decodeProxyPayload(req: IncomingMessage): SessionContextPayload {
  const encoded = req.headers["x-discord-proxy-payload"];
  const result: SessionContextPayload = { userId: null, displayName: null, guildId: null, channelId: null, instanceId: null };
  if (!encoded || Array.isArray(encoded)) return result;
  try {
    const decoded = JSON.parse(Buffer.from(encoded, "base64").toString("utf-8")) as Record<string, any>;
    const user = decoded.user ?? decoded.member?.user ?? null;
    result.userId = normalizeIntString(decoded.user_id ?? decoded.userId ?? decoded.discord_user_id ?? user?.id ?? (Array.isArray(decoded.users) ? decoded.users[0] : null));
    result.displayName = normalizeIntString(decoded.display_name ?? decoded.displayName ?? decoded.member?.nick ?? user?.global_name ?? user?.username) ?? null;
    result.guildId = normalizeIntString(decoded.guild_id ?? decoded.guildId ?? decoded.location?.guild_id);
    result.channelId = normalizeIntString(decoded.channel_id ?? decoded.channelId ?? decoded.location?.channel_id);
    result.instanceId = normalizeIntString(decoded.instance_id ?? decoded.instanceId);
  } catch {
    // ignore malformed proxy payloads and fall back to client hints
  }
  return result;
}

function mergeSession(base: SessionContextPayload, incoming: SessionContextPayload): SessionContextPayload {
  return {
    userId: incoming.userId ?? base.userId,
    displayName: incoming.displayName ?? base.displayName,
    guildId: incoming.guildId ?? base.guildId,
    channelId: incoming.channelId ?? base.channelId,
    instanceId: incoming.instanceId ?? base.instanceId,
  };
}

function mergeWithSession<T extends Record<string, any>>(payload: T, session: SessionContextPayload): T {
  return {
    ...payload,
    guildId: payload.guildId ?? session.guildId,
    channelId: payload.channelId ?? session.channelId,
    userId: payload.userId ?? session.userId,
    displayName: payload.displayName ?? session.displayName,
    instanceId: payload.instanceId ?? session.instanceId,
  };
}

interface BalanceLookupResult {
  balance: BalanceSnapshot;
  debug: BalanceDebugSnapshot;
}

async function fetchBalance(guildId: string, userId: string, session?: SessionContextPayload): Promise<BalanceLookupResult> {
  const coll = await ensureMongo();
  const query = { type: "user", guild_id: Number(guildId), user_id: Number(userId) };
  if (!coll) {
    return {
      balance: { chips: 0, bonusChips: 0 },
      debug: {
        source: "fallback_no_mongo",
        sessionUserId: session?.userId ?? null,
        sessionGuildId: session?.guildId ?? null,
        requestUserId: userId,
        requestGuildId: guildId,
        mongoConnected: false,
        mongoDbName,
        mongoCollectionName,
        query,
        docFound: false,
        docKeys: [],
        rawChips: null,
        rawBonusChips: null,
        normalizedChips: 0,
        normalizedBonusChips: 0,
        note: "mongo indisponível; usando fallback 0/0",
      },
    };
  }

  const doc = await coll.findOne(query, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
  const chips = Number(doc?.chips ?? 0);
  const bonusChips = Number(doc?.bonus_chips ?? 0);
  const balance = {
    chips: Number.isFinite(chips) ? chips : 0,
    bonusChips: Number.isFinite(bonusChips) ? bonusChips : 0,
  };
  const debug: BalanceDebugSnapshot = {
    source: doc ? "mongo_doc" : "mongo_default",
    sessionUserId: session?.userId ?? null,
    sessionGuildId: session?.guildId ?? null,
    requestUserId: userId,
    requestGuildId: guildId,
    mongoConnected: true,
    mongoDbName,
    mongoCollectionName,
    query,
    docFound: Boolean(doc),
    docKeys: doc ? Object.keys(doc).sort() : [],
    rawChips: doc?.chips ?? null,
    rawBonusChips: doc?.bonus_chips ?? null,
    normalizedChips: balance.chips,
    normalizedBonusChips: balance.bonusChips,
    note: doc ? "consulta executada" : "documento do usuário não encontrado com essa guild/user",
  };
  console.log("[sinuca-balance]", JSON.stringify(debug));
  return { balance, debug };
}

async function handleBalance(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const bodyGuildId = typeof req.body?.guildId === "string" ? req.body.guildId : null;
  const bodyUserId = typeof req.body?.userId === "string" ? req.body.userId : null;
  const queryGuildId = typeof req.query?.guildId === "string" ? req.query.guildId : null;
  const queryUserId = typeof req.query?.userId === "string" ? req.query.userId : null;
  const guildId = bodyGuildId ?? queryGuildId ?? session.guildId;
  const userId = bodyUserId ?? queryUserId ?? session.userId;

  console.log("[sinuca-balance-http]", JSON.stringify({
    method: req.method,
    url: req.url ?? null,
    sessionGuildId: session.guildId,
    sessionUserId: session.userId,
    bodyGuildId,
    bodyUserId,
    queryGuildId,
    queryUserId,
    resolvedGuildId: guildId,
    resolvedUserId: userId,
  }));

  if (!guildId || !userId) {
    const debug: BalanceDebugSnapshot = {
      source: "missing_identifiers",
      sessionUserId: session.userId ?? null,
      sessionGuildId: session.guildId ?? null,
      requestUserId: userId ?? null,
      requestGuildId: guildId ?? null,
      mongoConnected: Boolean(mongoUri),
      mongoDbName,
      mongoCollectionName,
      query: { type: "user", guild_id: guildId ? Number(guildId) : null, user_id: userId ? Number(userId) : null },
      docFound: false,
      docKeys: [],
      rawChips: null,
      rawBonusChips: null,
      normalizedChips: 0,
      normalizedBonusChips: 0,
      note: "guildId ou userId ausente no fallback HTTP",
    };
    res.status(200).json({ balance: { chips: 0, bonusChips: 0 }, debug });
    return;
  }

  try {
    const result = await fetchBalance(guildId, userId, session);
    res.json({ balance: result.balance, debug: result.debug });
  } catch (error) {
    console.error("[sinuca-balance-http-error]", error);
    const debug: BalanceDebugSnapshot = {
      source: "balance_error",
      sessionUserId: session.userId ?? null,
      sessionGuildId: session.guildId ?? null,
      requestUserId: userId,
      requestGuildId: guildId,
      mongoConnected: Boolean(mongoUri),
      mongoDbName,
      mongoCollectionName,
      query: { type: "user", guild_id: Number(guildId), user_id: Number(userId) },
      docFound: false,
      docKeys: [],
      rawChips: null,
      rawBonusChips: null,
      normalizedChips: 0,
      normalizedBonusChips: 0,
      note: "erro ao buscar saldo via fallback HTTP",
    };
    res.status(200).json({ balance: { chips: 0, bonusChips: 0 }, debug });
  }
}

app.get("/balance", handleBalance);
app.get("/api/balance", handleBalance);
app.post("/balance", handleBalance);
app.post("/api/balance", handleBalance);

function watchBalance(ws: WebSocket, guildId: string | null | undefined, userId: string | null | undefined) {
  if (!guildId || !userId) {
    balanceWatchers.delete(ws);
    return;
  }
  balanceWatchers.set(ws, { guildId, userId, lastSent: "" });
}

async function pushBalance(ws: WebSocket, guildId: string, userId: string, force = false) {
  const session = socketSession.get(ws);
  try {
    const result = await fetchBalance(guildId, userId, session);
    const nextKey = JSON.stringify(result.balance);
    const current = balanceWatchers.get(ws);
    if (!current) return;
    if (!force && current.lastSent === nextKey) return;
    current.lastSent = nextKey;
    console.log("[sinuca-balance-push]", JSON.stringify({ guildId, userId, force, nextKey, source: result.debug.source, docFound: result.debug.docFound }));
    send(ws, { type: "balance_state", payload: result.balance });
    send(ws, { type: "balance_debug", payload: result.debug });
  } catch (error) {
    console.error("[sinuca-balance-error]", error);
    if (force) {
      send(ws, { type: "balance_state", payload: { chips: 0, bonusChips: 0 } });
      send(ws, { type: "balance_debug", payload: {
        source: "balance_error",
        sessionUserId: session?.userId ?? null,
        sessionGuildId: session?.guildId ?? null,
        requestUserId: userId,
        requestGuildId: guildId,
        mongoConnected: Boolean(mongoUri),
        mongoDbName,
        mongoCollectionName,
        query: { type: "user", guild_id: Number(guildId), user_id: Number(userId) },
        docFound: false,
        docKeys: [],
        rawChips: null,
        rawBonusChips: null,
        normalizedChips: 0,
        normalizedBonusChips: 0,
        note: "erro ao buscar saldo",
      } });
    }
  }
}

const balanceTicker = setInterval(() => {
  for (const [ws, watch] of balanceWatchers.entries()) {
    if (ws.readyState !== 1) continue;
    void pushBalance(ws, watch.guildId, watch.userId);
  }
}, 2000);

wss.on("connection", (ws: WebSocket, req: IncomingMessage) => {
  const resolved = resolveRequestSession(req);
  const session: SessionContextPayload = {
    userId: resolved.userId,
    displayName: resolved.displayName,
    guildId: resolved.guildId,
    channelId: resolved.channelId,
    instanceId: resolved.instanceId,
  };
  socketSession.set(ws, session);
  send(ws, { type: "ready" });
  send(ws, { type: "session_context", payload: session });
  console.log("[sinuca-session]", JSON.stringify({
    userId: session.userId,
    displayName: session.displayName,
    guildId: session.guildId,
    channelId: session.channelId,
    instanceId: session.instanceId,
    sessionSource: resolved.sessionSource,
    proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
    origin: req.headers.origin ?? null,
    ua: req.headers["user-agent"] ?? null,
    url: req.url ?? null,
  }));
  if (session.guildId && session.userId) {
    watchBalance(ws, session.guildId, session.userId);
    void pushBalance(ws, session.guildId, session.userId, true);
  }

  ws.on("message", async (raw: unknown) => {
    let data: ClientMessage;
    try {
      const rawText = typeof raw === "string" ? raw : (raw as { toString?: (encoding?: string) => string })?.toString?.("utf-8") ?? String(raw);
      data = JSON.parse(rawText) as ClientMessage;
    } catch {
      send(ws, { type: "error", message: "payload inválido" });
      return;
    }

    const activeSession = socketSession.get(ws) ?? session;

    if (data.type === "ping") {
      send(ws, { type: "pong" });
      return;
    }

    if (data.type === "exchange_token") {
      const code = typeof data.payload?.code === "string" ? data.payload.code : "";
      const result = await exchangeDiscordCode(code);
      send(ws, {
        type: "oauth_token_result",
        payload: {
          ok: result.ok,
          accessToken: result.accessToken,
          error: result.error,
          detail: result.detail,
        },
      });
      return;
    }

    if (data.type === "init_context") {
      console.log("[sinuca-init-context]", JSON.stringify(data.payload));
      const nextSession = mergeSession(activeSession, data.payload);
      socketSession.set(ws, nextSession);
      send(ws, { type: "session_context", payload: nextSession });
      console.log("[sinuca-session-client]", JSON.stringify(nextSession));
      if (nextSession.guildId && nextSession.userId) {
        watchBalance(ws, nextSession.guildId, nextSession.userId);
        await pushBalance(ws, nextSession.guildId, nextSession.userId, true);
      }
      return;
    }

    if (data.type === "list_rooms") {
      const merged = mergeWithSession(data.payload, activeSession);
      watchContext(ws, merged);
      send(ws, { type: "room_list", payload: listRooms(merged).map(toSnapshot) });
      return;
    }

    if (data.type === "get_balance") {
      const merged = mergeWithSession(data.payload, activeSession);
      console.log("[sinuca-get-balance]", JSON.stringify({ activeSession, request: data.payload, merged }));
      if (!merged.guildId || !merged.userId) {
        send(ws, { type: "balance_state", payload: { chips: 0, bonusChips: 0 } });
        send(ws, { type: "balance_debug", payload: {
          source: "missing_identifiers",
          sessionUserId: activeSession.userId ?? null,
          sessionGuildId: activeSession.guildId ?? null,
          requestUserId: merged.userId ?? null,
          requestGuildId: merged.guildId ?? null,
          mongoConnected: Boolean(mongoUri),
          mongoDbName,
          mongoCollectionName,
          query: { type: "user", guild_id: merged.guildId ? Number(merged.guildId) : null, user_id: merged.userId ? Number(merged.userId) : null },
          docFound: false,
          docKeys: [],
          rawChips: null,
          rawBonusChips: null,
          normalizedChips: 100,
          normalizedBonusChips: 0,
          note: "guildId ou userId ausente na sessão/request",
        } });
        return;
      }
      watchBalance(ws, merged.guildId, merged.userId);
      await pushBalance(ws, merged.guildId, merged.userId, true);
      return;
    }

    if (data.type === "create_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      const { instanceId, guildId, channelId, userId, displayName } = merged;
      if (!instanceId || !userId || !displayName) {
        send(ws, { type: "error", message: "sessão da activity incompleta" });
        return;
      }
      const room = createRoom(instanceId, guildId ?? null, channelId ?? null, userId, displayName);
      subscribeSocket(room.roomId, ws);
      send(ws, { type: "room_state", payload: toSnapshot(room) });
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      return;
    }

    if (data.type === "join_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      if (!merged.userId || !merged.displayName) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = addPlayer(merged.roomId, merged.userId, merged.displayName);
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      subscribeSocket(room.roomId, ws);
      broadcastRoom(room.roomId);
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      return;
    }

    if (data.type === "leave_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      const previous = getRoom(merged.roomId);
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = removePlayer(merged.roomId, merged.userId);
      unsubscribeSocket(ws);
      if (room) {
        broadcastRoom(room.roomId);
        broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      } else if (previous) {
        broadcastRoomList({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode });
      }
      return;
    }

    if (data.type === "set_ready") {
      const merged = mergeWithSession(data.payload, activeSession);
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = setPlayerReady(merged.roomId, merged.userId, merged.ready);
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      broadcastRoom(room.roomId);
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
    }
  });

  ws.on("close", () => {
    unsubscribeSocket(ws);
    unwatchContext(ws);
    balanceWatchers.delete(ws);
    socketSession.delete(ws);
  });
});

server.on("close", () => clearInterval(balanceTicker));

const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[sinuca-server] ouvindo na porta ${port}`);
});
