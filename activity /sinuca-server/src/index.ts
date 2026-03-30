import express from "express";
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
app.get("/health", (_req, res) => {
  res.json({ ok: true, rules: getInitialRuleSet() });
});

const server = createServer(app);
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

function decodeProxyPayload(req: IncomingMessage): SessionContextPayload {
  const encoded = req.headers["x-discord-proxy-payload"];
  const result: SessionContextPayload = {
    userId: null,
    displayName: null,
    guildId: null,
    channelId: null,
    instanceId: null,
  };
  if (!encoded || Array.isArray(encoded)) return result;
  try {
    const decoded = JSON.parse(Buffer.from(encoded, "base64").toString("utf-8")) as Record<string, any>;
    const user = decoded.user ?? decoded.member?.user ?? null;
    result.userId = normalizeIntString(decoded.user_id ?? decoded.userId ?? decoded.discord_user_id ?? user?.id ?? (Array.isArray(decoded.users) ? decoded.users[0] : null));
    result.displayName =
      normalizeIntString(decoded.display_name ?? decoded.displayName ?? decoded.member?.nick ?? user?.global_name ?? user?.username) ?? null;
    result.guildId = normalizeIntString(decoded.guild_id ?? decoded.guildId ?? decoded.location?.guild_id);
    result.channelId = normalizeIntString(decoded.channel_id ?? decoded.channelId ?? decoded.location?.channel_id);
    result.instanceId = normalizeIntString(decoded.instance_id ?? decoded.instanceId);
  } catch {
    // ignore malformed proxy payloads and fall back to client hints
  }
  return result;
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
      balance: { chips: 100, bonusChips: 0 },
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
        normalizedChips: 100,
        normalizedBonusChips: 0,
        note: "mongo indisponível; usando fallback 100/0",
      },
    };
  }
  const doc = await coll.findOne(query, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
  const chips = Number(doc?.chips ?? 100);
  const bonusChips = Number(doc?.bonus_chips ?? 0);
  const balance = {
    chips: Number.isFinite(chips) ? chips : 100,
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

function watchBalance(ws: WebSocket, guildId: string | null | undefined, userId: string | null | undefined) {
  if (!guildId || !userId) {
    balanceWatchers.delete(ws);
    return;
  }
  balanceWatchers.set(ws, { guildId, userId, lastSent: "" });
}

async function pushBalance(ws: WebSocket, guildId: string, userId: string, force = false) {
  try {
    const session = socketSession.get(ws);
    const result = await fetchBalance(guildId, userId, session);
    const nextKey = JSON.stringify(result.balance);
    const current = balanceWatchers.get(ws);
    if (!current) return;
    if (!force && current.lastSent === nextKey) return;
    current.lastSent = nextKey;
    send(ws, { type: "balance_state", payload: result.balance });
    send(ws, { type: "balance_debug", payload: result.debug });
  } catch (error) {
    console.error("[sinuca-balance-error]", error);
    if (force) {
      send(ws, { type: "balance_state", payload: { chips: 100, bonusChips: 0 } });
      send(ws, { type: "balance_debug", payload: {
        source: "exception",
        sessionUserId: socketSession.get(ws)?.userId ?? null,
        sessionGuildId: socketSession.get(ws)?.guildId ?? null,
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
        normalizedChips: 100,
        normalizedBonusChips: 0,
        note: error instanceof Error ? error.message : "erro desconhecido ao buscar saldo",
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

wss.on("connection", (ws, req) => {
  const session = decodeProxyPayload(req);
  socketSession.set(ws, session);
  send(ws, { type: "ready" });
  send(ws, { type: "session_context", payload: session });
  console.log("[sinuca-session]", JSON.stringify({
    userId: session.userId,
    displayName: session.displayName,
    guildId: session.guildId,
    channelId: session.channelId,
    instanceId: session.instanceId,
    proxyPayload: req.headers["x-discord-proxy-payload"] ? "present" : "missing",
    origin: req.headers.origin ?? null,
    ua: req.headers["user-agent"] ?? null,
  }));
  if (session.guildId && session.userId) {
    watchBalance(ws, session.guildId, session.userId);
    void pushBalance(ws, session.guildId, session.userId, true);
  }

  ws.on("message", async (raw) => {
    let data: ClientMessage;
    try {
      data = JSON.parse(raw.toString()) as ClientMessage;
    } catch {
      send(ws, { type: "error", message: "payload inválido" });
      return;
    }

    const activeSession = socketSession.get(ws) ?? session;

    if (data.type === "ping") {
      send(ws, { type: "pong" });
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
      if (!merged.guildId || !merged.userId) {
        send(ws, { type: "balance_state", payload: { chips: 100, bonusChips: 0 } });
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

server.on("close", () => {
  clearInterval(balanceTicker);
});

const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[sinuca-server] ouvindo na porta ${port}`);
});
