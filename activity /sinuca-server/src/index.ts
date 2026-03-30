import express from "express";
import { createServer } from "http";
import { MongoClient } from "mongodb";
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
import type { BalanceSnapshot, ClientMessage, ListRoomsPayload, ServerMessage } from "./messages.js";
import { getInitialRuleSet } from "./gameRules.js";

const app = express();
app.get("/health", (_req, res) => {
  res.json({ ok: true, rules: getInitialRuleSet() });
});

const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });
const contextWatchers = new Map<string, Set<WebSocket>>();
const socketContext = new Map<WebSocket, string>();

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

async function fetchBalance(guildId: string, userId: string): Promise<BalanceSnapshot> {
  const coll = await ensureMongo();
  if (!coll) return { chips: 0, bonusChips: 0 };
  const doc = await coll.findOne({ type: "user", guild_id: Number(guildId), user_id: Number(userId) }, { projection: { chips: 1, bonus_chips: 1 } });
  const chips = Number(doc?.chips ?? 100);
  const bonusChips = Number(doc?.bonus_chips ?? 0);
  return {
    chips: Number.isFinite(chips) ? chips : 100,
    bonusChips: Number.isFinite(bonusChips) ? bonusChips : 0,
  };
}

wss.on("connection", (ws) => {
  send(ws, { type: "ready" });

  ws.on("message", async (raw) => {
    let data: ClientMessage;
    try {
      data = JSON.parse(raw.toString()) as ClientMessage;
    } catch {
      send(ws, { type: "error", message: "payload inválido" });
      return;
    }

    if (data.type === "ping") {
      send(ws, { type: "pong" });
      return;
    }

    if (data.type === "list_rooms") {
      watchContext(ws, data.payload);
      send(ws, { type: "room_list", payload: listRooms(data.payload).map(toSnapshot) });
      return;
    }

    if (data.type === "get_balance") {
      try {
        send(ws, { type: "balance_state", payload: await fetchBalance(data.payload.guildId, data.payload.userId) });
      } catch {
        send(ws, { type: "balance_state", payload: { chips: 100, bonusChips: 0 } });
      }
      return;
    }

    if (data.type === "create_room") {
      const { instanceId, guildId, channelId, userId, displayName } = data.payload;
      const room = createRoom(instanceId, guildId, channelId, userId, displayName);
      subscribeSocket(room.roomId, ws);
      send(ws, { type: "room_state", payload: toSnapshot(room) });
      broadcastRoomList({ guildId, channelId, mode: room.mode });
      return;
    }

    if (data.type === "join_room") {
      const room = addPlayer(data.payload.roomId, data.payload.userId, data.payload.displayName);
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
      const previous = getRoom(data.payload.roomId);
      const room = removePlayer(data.payload.roomId, data.payload.userId);
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
      const room = setPlayerReady(data.payload.roomId, data.payload.userId, data.payload.ready);
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
  });
});

const port = Number(process.env.PORT || 8787);
server.listen(port, () => {
  console.log(`[sinuca-server] ouvindo na porta ${port}`);
});
