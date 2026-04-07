import express, { type Request, type Response } from "express";
import { createServer } from "http";
import { Long, MongoClient } from "mongodb";
import type { IncomingMessage } from "http";
import type { WebSocket } from "ws";
import { WebSocketServer } from "ws";
import {
  addPlayer,
  createRoom,
  getRoom,
  getSubscribers,
  closeRoom,
  listRooms,
  removePlayer,
  setPlayerReady,
  setRoomInGame,
  setRoomStake,
  subscribeSocket,
  toSnapshot,
  unsubscribeSocket,
} from "./rooms.js";
import type { RoomRecord } from "./rooms.js";
import type {
  AimPointerMode,
  AimStateSnapshot,
  BalanceDebugSnapshot,
  BalanceSnapshot,
  ClientMessage,
  ListRoomsPayload,
  ServerMessage,
  SessionContextPayload,
} from "./messages.js";
import { getInitialRuleSet } from "./gameRules.js";
import { getGameSnapshot, removeGame, startGameForRoom, stepRealtimeGames, takeShot } from "./gameState.js";

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

function sendNoStoreJson(res: Response, payload: unknown) {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  res.json(payload);
}

function handleHealth(req: Request, res: Response) {
  console.log("[sinuca-health]", JSON.stringify({ origin: req.headers.origin ?? null, ua: req.headers["user-agent"] ?? null, url: req.url ?? null }));
  sendNoStoreJson(res, { ok: true, rules: getInitialRuleSet() });
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
const latestAimByRoom = new Map<string, AimStateSnapshot>();
const aimRevisionByRoom = new Map<string, number>();
const roomActivityAt = new Map<string, number>();
const ROOM_IDLE_TIMEOUT_MS = 5 * 60 * 1000;

const mongoUri = process.env.MONGODB_URI || process.env.MONGO_URI || "";
const mongoDbName = process.env.MONGODB_DB || process.env.MONGO_DB_NAME || process.env.MONGODB_DB_NAME || "chat_revive";
const mongoCollectionName = process.env.MONGODB_COLLECTION || process.env.MONGO_COLLECTION_NAME || process.env.MONGODB_COLLECTION_NAME || "settings";
let mongoClient: MongoClient | null = null;

function send(ws: WebSocket, payload: ServerMessage) {
  ws.send(JSON.stringify(payload));
}

function touchRoomActivity(roomId: string, source: string) {
  roomActivityAt.set(roomId, Date.now());
  console.log("[sinuca-room-activity]", JSON.stringify({ roomId, source, at: roomActivityAt.get(roomId) }));
}

function clearRoomActivity(roomId: string) {
  roomActivityAt.delete(roomId);
}

function notifyRoomClosed(room: RoomRecord, reason: string, message: string) {
  for (const subscriber of getSubscribers(room.roomId)) {
    if (subscriber.readyState !== 1) continue;
    send(subscriber, {
      type: "room_closed",
      payload: {
        roomId: room.roomId,
        reason,
        message,
      },
    });
  }
}

function closeRoomAndNotify(roomId: string, reason: string, message: string) {
  const room = getRoom(roomId);
  if (!room) {
    clearRoomActivity(roomId);
    latestAimByRoom.delete(roomId);
    aimRevisionByRoom.delete(roomId);
    removeGame(roomId);
    return null;
  }
  notifyRoomClosed(room, reason, message);
  latestAimByRoom.delete(roomId);
  aimRevisionByRoom.delete(roomId);
  removeGame(roomId);
  clearRoomActivity(roomId);
  const closedRoom = closeRoom(roomId);
  if (closedRoom) {
    broadcastRoomList({ guildId: closedRoom.guildId, channelId: closedRoom.channelId, mode: closedRoom.mode });
  }
  return closedRoom;
}

function storeAimState(roomId: string, payload: AimStateSnapshot) {
  aimRevisionByRoom.set(roomId, payload.snapshotRevision);
  latestAimByRoom.set(roomId, payload);
}

function clearAimState(roomId: string, userId?: string | null) {
  const current = latestAimByRoom.get(roomId);
  if (!current) return;
  if (userId && current.userId !== userId) return;
  const nextRevision = (aimRevisionByRoom.get(roomId) ?? current.snapshotRevision ?? 0) + 1;
  latestAimByRoom.set(roomId, {
    ...current,
    visible: false,
    power: 0,
    mode: "idle",
    updatedAt: Date.now(),
    seq: current.seq + 1,
    snapshotRevision: nextRevision,
  });
  aimRevisionByRoom.set(roomId, nextRevision);
}

function buildAimPayload(input: {
  roomId: string;
  userId: string;
  visible: boolean;
  angle?: unknown;
  cueX?: unknown;
  cueY?: unknown;
  power?: unknown;
  seq?: unknown;
  mode?: unknown;
}) {
  const nextRevision = (aimRevisionByRoom.get(input.roomId) ?? latestAimByRoom.get(input.roomId)?.snapshotRevision ?? 0) + 1;
  return {
    roomId: input.roomId,
    userId: input.userId,
    visible: Boolean(input.visible),
    angle: Number.isFinite(Number(input.angle)) ? Number(input.angle) : 0,
    cueX: input.cueX === undefined || input.cueX === null || !Number.isFinite(Number(input.cueX)) ? null : Number(input.cueX),
    cueY: input.cueY === undefined || input.cueY === null || !Number.isFinite(Number(input.cueY)) ? null : Number(input.cueY),
    power: input.power === undefined || input.power === null || !Number.isFinite(Number(input.power)) ? 0 : Number(input.power),
    seq: input.seq === undefined || input.seq === null || !Number.isFinite(Number(input.seq)) ? 0 : Number(input.seq),
    mode: normalizeAimMode(input.mode),
    updatedAt: Date.now(),
    snapshotRevision: nextRevision,
  } satisfies AimStateSnapshot;
}

function contextKey(payload: ListRoomsPayload) {
  if (payload.mode === "server") {
    return `${payload.mode}:${payload.guildId ?? ""}`;
  }
  return `${payload.mode}:${payload.guildId ?? ""}:${payload.channelId ?? ""}`;
}

function watchContext(ws: WebSocket, payload: ListRoomsPayload) {
  const nextKey = contextKey(payload);
  console.log("[sinuca-watch-context]", JSON.stringify({ nextKey, payload }));
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
  const key = contextKey(payload);
  const nextRooms = listRooms(payload).map(toSnapshot);
  const watchers = contextWatchers.get(key);
  console.log("[sinuca-broadcast-room-list]", JSON.stringify({ key, payload, rooms: nextRooms.map((room) => ({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, players: room.players.length, status: room.status, tableType: room.tableType, stakeChips: room.stakeChips })), watcherCount: watchers?.size ?? 0 }));
  if (!watchers || watchers.size === 0) return;
  const message: ServerMessage = { type: "room_list", payload: nextRooms };
  for (const client of watchers) {
    send(client, message);
  }
}


function broadcastGame(roomId: string) {
  const game = getGameSnapshot(roomId);
  if (!game) return;
  noteRealtimeBroadcast(roomId, game.snapshotRevision ?? 0, game.status);
  const payload: ServerMessage = { type: "game_state", payload: game };
  for (const client of getSubscribers(roomId)) {
    send(client, payload);
  }
}

function normalizeAimMode(value: unknown): AimPointerMode {
  return value === "aim" || value === "place" || value === "power" || value === "idle" ? value : "idle";
}

const pendingRealtimeBroadcastRooms = new Set<string>();
const realtimeDebugByRoom = new Map<string, { lastLogAt: number; lastStepAt: number; lastBroadcastAt: number; stepCount: number; broadcastCount: number; maxStepGapMs: number; maxBroadcastGapMs: number; }>();

function noteRealtimeStep(roomId: string) {
  const now = Date.now();
  const current = realtimeDebugByRoom.get(roomId) ?? { lastLogAt: now, lastStepAt: now, lastBroadcastAt: now, stepCount: 0, broadcastCount: 0, maxStepGapMs: 0, maxBroadcastGapMs: 0 };
  const stepGap = current.lastStepAt ? now - current.lastStepAt : 0;
  current.maxStepGapMs = Math.max(current.maxStepGapMs, stepGap);
  current.lastStepAt = now;
  current.stepCount += 1;
  realtimeDebugByRoom.set(roomId, current);
}

function noteRealtimeBroadcast(roomId: string, revision: number, status: string) {
  const now = Date.now();
  const current = realtimeDebugByRoom.get(roomId) ?? { lastLogAt: now, lastStepAt: now, lastBroadcastAt: now, stepCount: 0, broadcastCount: 0, maxStepGapMs: 0, maxBroadcastGapMs: 0 };
  const broadcastGap = current.lastBroadcastAt ? now - current.lastBroadcastAt : 0;
  current.maxBroadcastGapMs = Math.max(current.maxBroadcastGapMs, broadcastGap);
  current.lastBroadcastAt = now;
  current.broadcastCount += 1;
  if (now - current.lastLogAt >= 1000) {
    console.log('[sinuca-realtime-debug]', JSON.stringify({ roomId, status, revision, stepsPerWindow: current.stepCount, broadcastsPerWindow: current.broadcastCount, maxStepGapMs: current.maxStepGapMs, maxBroadcastGapMs: current.maxBroadcastGapMs, pendingRooms: pendingRealtimeBroadcastRooms.size }));
    current.lastLogAt = now;
    current.stepCount = 0;
    current.broadcastCount = 0;
    current.maxStepGapMs = 0;
    current.maxBroadcastGapMs = 0;
  }
  realtimeDebugByRoom.set(roomId, current);
}

const realtimeStepInterval = setInterval(() => {
  const changedRooms = stepRealtimeGames();
  for (const roomId of changedRooms) {
    noteRealtimeStep(roomId);
    pendingRealtimeBroadcastRooms.add(roomId);
  }
}, 1000 / 60);
realtimeStepInterval.unref?.();

const realtimeBroadcastInterval = setInterval(() => {
  if (!pendingRealtimeBroadcastRooms.size) return;
  const roomIds = [...pendingRealtimeBroadcastRooms];
  pendingRealtimeBroadcastRooms.clear();
  for (const roomId of roomIds) {
    broadcastGame(roomId);
  }
}, 1000 / 60);
realtimeBroadcastInterval.unref?.();

function broadcastAim(roomId: string, payload: AimStateSnapshot, except?: WebSocket | null) {
  const message: ServerMessage = { type: "aim_state", payload };
  for (const client of getSubscribers(roomId)) {
    if (except && client === except) continue;
    send(client, message);
  }
}

function normalizeRoomMode(value: unknown, fallbackGuildId?: string | null): "server" | "casual" {
  if (value === "server") return "server";
  if (value === "casual") return "casual";
  return fallbackGuildId ? "server" : "casual";
}

async function handleListRoomsHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const payload = {
    mode: normalizeRoomMode(firstString(req.body?.mode) ?? firstString(req.query?.mode), session.guildId),
    guildId: normalizeIntString(firstString(req.body?.guildId) ?? firstString(req.query?.guildId)) ?? session.guildId,
    channelId: normalizeIntString(firstString(req.body?.channelId) ?? firstString(req.query?.channelId)) ?? session.channelId,
  } satisfies ListRoomsPayload;
  const rooms = listRooms(payload).map(toSnapshot);
  console.log("[sinuca-list-rooms-http]", JSON.stringify({ payload, session, rooms: rooms.map((room) => ({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, players: room.players.length, status: room.status, tableType: room.tableType, stakeChips: room.stakeChips })) }));
  sendNoStoreJson(res, { rooms });
}

async function handleGetRoomHttp(req: Request, res: Response) {
  const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
  if (!roomId) {
    res.status(400).json({ error: "missing_room_id" });
    return;
  }
  const room = getRoom(roomId);
  console.log("[sinuca-get-room-http]", JSON.stringify({ roomId, found: Boolean(room) }));
  sendNoStoreJson(res, { room: room ? toSnapshot(room) : null });
}

async function handleCreateRoomHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const instanceId = normalizeIntString(merged.instanceId);
  const guildId = normalizeIntString(merged.guildId);
  const channelId = normalizeIntString(merged.channelId);
  const userId = normalizeIntString(merged.userId);
  const displayName = firstString(merged.displayName);
  const avatarUrl = firstString(merged.avatarUrl);
  const requestedTableType = merged.tableType === "casual" ? "casual" : "stake";
  const stakeChips = typeof merged.stakeChips === "number" ? merged.stakeChips : Number(merged.stakeChips ?? 0);
  console.log("[sinuca-create-room-http-request]", JSON.stringify({ session, merged: { instanceId, guildId, channelId, userId, displayName, avatarUrl, tableType: requestedTableType, stakeChips } }));
  if (!instanceId || !userId || !displayName) {
    res.status(400).json({ error: "incomplete_session" });
    return;
  }
  const room = createRoom(instanceId, guildId, channelId, userId, displayName, avatarUrl ?? null, {
    tableType: requestedTableType,
    stakeChips: Number.isFinite(stakeChips) ? stakeChips : null,
  });
  touchRoomActivity(room.roomId, "http_create_room");
  broadcastRoom(room.roomId);
  broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  sendNoStoreJson(res, { room: toSnapshot(room) });
}

async function handleJoinRoomHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  const displayName = firstString(merged.displayName);
  const avatarUrl = firstString(merged.avatarUrl);
  console.log("[sinuca-join-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, displayName, avatarUrl } }));
  if (!roomId || !userId || !displayName) {
    res.status(400).json({ error: "missing_join_identifiers" });
    return;
  }
  const room = addPlayer(roomId, userId, displayName, avatarUrl ?? null);
  if (!room) {
    res.status(404).json({ error: "room_not_found" });
    return;
  }
  touchRoomActivity(room.roomId, "http_join_room");
  broadcastRoom(room.roomId);
  broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  sendNoStoreJson(res, { room: toSnapshot(room) });
}

async function handleLeaveRoomHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  const shouldCloseRoom = booleanish(merged.closeRoom, false);
  console.log("[sinuca-leave-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, closeRoom: shouldCloseRoom } }));
  if (!roomId || !userId) {
    res.status(400).json({ error: "missing_leave_identifiers" });
    return;
  }
  const previous = getRoom(roomId);
  if (previous?.status === "in_game") {
    removeGame(roomId);
    setRoomInGame(roomId, false);
  }
  const room = shouldCloseRoom && previous && previous.hostUserId === userId
    ? null
    : removePlayer(roomId, userId);
  const closedRoom = shouldCloseRoom && previous && previous.hostUserId === userId
    ? closeRoomAndNotify(roomId, "host_closed_room", "A sala foi fechada pelo anfitrião.")
    : null;
  if (room) {
    touchRoomActivity(room.roomId, "http_leave_room_remaining");
    broadcastRoom(room.roomId);
    broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  } else if (!closedRoom && previous) {
    clearRoomActivity(previous.roomId);
    latestAimByRoom.delete(previous.roomId);
    aimRevisionByRoom.delete(previous.roomId);
    broadcastRoomList({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode });
  }
  sendNoStoreJson(res, { room: room ? toSnapshot(room) : null, closed: Boolean(closedRoom) });
}

async function handleReadyRoomHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  const ready = booleanish(merged.ready, false);
  console.log("[sinuca-ready-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, ready } }));
  if (!roomId || !userId) {
    res.status(400).json({ error: "missing_ready_identifiers" });
    return;
  }
  const room = setPlayerReady(roomId, userId, ready);
  if (!room) {
    res.status(404).json({ error: "room_not_found" });
    return;
  }
  touchRoomActivity(room.roomId, "http_set_ready");
  broadcastRoom(room.roomId);
  broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  sendNoStoreJson(res, { room: toSnapshot(room) });
}

async function handleUpdateStakeRoomHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  const rawStake = typeof merged.stakeChips === "number" ? merged.stakeChips : Number(merged.stakeChips ?? 0);
  const tableType = merged.tableType === "casual" || rawStake === 0 ? "casual" : "stake";
  console.log("[sinuca-stake-room-http-request]", JSON.stringify({ session, merged: { roomId, userId, tableType, stakeChips: rawStake } }));
  if (!roomId || !userId) {
    res.status(400).json({ error: "missing_stake_identifiers" });
    return;
  }
  const currentRoom = getRoom(roomId);
  if (!currentRoom) {
    res.status(404).json({ error: "room_not_found" });
    return;
  }
  if (currentRoom.hostUserId !== userId) {
    res.status(403).json({ error: "only_host_can_update_stake" });
    return;
  }
  const room = setRoomStake(roomId, userId, { tableType, stakeChips: Number.isFinite(rawStake) ? rawStake : null });
  if (!room) {
    res.status(404).json({ error: "room_not_found" });
    return;
  }
  touchRoomActivity(room.roomId, "http_update_stake");
  broadcastRoom(room.roomId);
  broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  sendNoStoreJson(res, { room: toSnapshot(room) });
}

async function handleGetAimHttp(req: Request, res: Response) {
  const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
  if (!roomId) {
    res.status(400).json({ error: "missing_room_id" });
    return;
  }
  sendNoStoreJson(res, { aim: latestAimByRoom.get(roomId) ?? null });
}

async function handleSyncAimHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  if (!roomId || !userId) {
    res.status(400).json({ error: "missing_aim_identifiers" });
    return;
  }
  const room = getRoom(roomId);
  const game = getGameSnapshot(roomId);
  if (!room || !game) {
    res.status(404).json({ error: "game_not_found" });
    return;
  }
  const payload = buildAimPayload({
    roomId,
    userId,
    visible: Boolean(merged.visible),
    angle: merged.angle,
    cueX: merged.cueX,
    cueY: merged.cueY,
    power: merged.power,
    seq: merged.seq,
    mode: merged.mode,
  });
  if (payload.visible && game.turnUserId !== userId) {
    res.status(409).json({ error: "not_your_turn", aim: latestAimByRoom.get(roomId) ?? null });
    return;
  }
  storeAimState(roomId, payload);
  touchRoomActivity(roomId, "http_sync_aim");
  broadcastAim(roomId, payload);
  res.json({ aim: payload });
}

async function handleGetGameHttp(req: Request, res: Response) {
  const roomId = normalizeIntString(req.params?.roomId ?? firstString(req.body?.roomId) ?? firstString(req.query?.roomId));
  const sinceSeq = Number(firstString(req.body?.sinceSeq) ?? firstString(req.query?.sinceSeq) ?? 0);
  const session = resolveRequestSession(req);
  console.log('[sinuca-game-snapshot-http]', JSON.stringify({
    method: req.method,
    url: req.url ?? null,
    roomId,
    sinceSeq: Number.isFinite(sinceSeq) ? sinceSeq : 0,
    userId: session.userId,
    guildId: session.guildId,
    instanceId: session.instanceId,
  }));
  if (!roomId) {
    console.log('[sinuca-game-snapshot-http-rejected]', JSON.stringify({ reason: 'missing_room_id', url: req.url ?? null }));
    res.status(400).json({ error: "missing_room_id" });
    return;
  }
  const game = getGameSnapshot(roomId, Number.isFinite(sinceSeq) ? sinceSeq : 0);
  console.log('[sinuca-game-snapshot-http-result]', JSON.stringify({
    roomId,
    hasGame: Boolean(game),
    gameId: game?.gameId ?? null,
    shotSequence: game?.shotSequence ?? null,
    status: game?.status ?? null,
  }));
  sendNoStoreJson(res, { game });
}

async function handleStartGameHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  console.log("[sinuca-start-http]", JSON.stringify({
    method: req.method,
    url: req.url ?? null,
    roomId,
    userId,
    sessionUserId: session.userId,
    sessionGuildId: session.guildId,
  }));
  if (!roomId || !userId) {
    console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "missing_start_identifiers" }));
    res.status(400).json({ error: "missing_start_identifiers" });
    return;
  }
  const room = getRoom(roomId);
  if (!room) {
    console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "room_not_found" }));
    res.status(404).json({ error: "room_not_found" });
    return;
  }
  if (room.hostUserId !== userId) {
    console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "only_host_can_start", hostUserId: room.hostUserId }));
    res.status(403).json({ error: "only_host_can_start" });
    return;
  }
  const opponent = room.players.find((player) => player.userId !== room.hostUserId);
  if (!opponent || !opponent.ready) {
    console.log("[sinuca-start-http-rejected]", JSON.stringify({ roomId, userId, reason: "room_not_ready", opponentUserId: opponent?.userId ?? null, opponentReady: opponent?.ready ?? null }));
    res.status(409).json({ error: "room_not_ready" });
    return;
  }
  setRoomInGame(roomId, true);
  const game = startGameForRoom(room);
  latestAimByRoom.delete(roomId);
  touchRoomActivity(roomId, "http_start_game");
  broadcastRoom(roomId);
  broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
  broadcastGame(roomId);
  console.log("[sinuca-start-http-applied]", JSON.stringify({ roomId, userId, turnUserId: game.turnUserId, phase: game.phase, shotSequence: game.shotSequence }));
  res.json({ game, room: toSnapshot(getRoom(roomId) ?? room) });
}

async function handleShootGameHttp(req: Request, res: Response) {
  const session = resolveRequestSession(req);
  const merged = mergeWithSession({ ...(req.query ?? {}), ...(req.body ?? {}) }, session);
  const roomId = normalizeIntString(merged.roomId);
  const userId = normalizeIntString(merged.userId);
  const angle = Number(merged.angle ?? 0);
  const power = Number(merged.power ?? 0);
  const cueX = merged.cueX === undefined ? null : Number(merged.cueX);
  const cueY = merged.cueY === undefined ? null : Number(merged.cueY);
  const calledPocket = merged.calledPocket === undefined ? null : Number(merged.calledPocket);
  const spinX = merged.spinX === undefined ? 0 : Number(merged.spinX);
  const spinY = merged.spinY === undefined ? 0 : Number(merged.spinY);
  console.log("[sinuca-shoot-http]", JSON.stringify({
    method: req.method,
    roomId,
    userId,
    angle,
    power,
    cueX,
    cueY,
    calledPocket,
    spinX,
    spinY,
  }));
  if (!roomId || !userId) {
    res.status(400).json({ error: "missing_shot_identifiers" });
    return;
  }
  const room = getRoom(roomId);
  if (!room || room.status !== "in_game") {
    res.status(404).json({ error: "game_not_found" });
    return;
  }
  const before = getGameSnapshot(roomId);
  if (!before) {
    res.status(404).json({ error: "game_not_found" });
    return;
  }
  if (before.turnUserId !== userId) {
    console.log("[sinuca-shoot-http-rejected]", JSON.stringify({ roomId, userId, reason: "not_your_turn", turnUserId: before.turnUserId }));
    res.status(409).json({ error: "not_your_turn", game: before });
    return;
  }
  const game = takeShot(
    roomId,
    userId,
    angle,
    power,
    cueX,
    cueY,
    Number.isFinite(calledPocket) ? calledPocket : null,
    Number.isFinite(spinX) ? spinX : 0,
    Number.isFinite(spinY) ? spinY : 0,
  );
  if (!game) {
    res.status(404).json({ error: "game_not_found" });
    return;
  }
  clearAimState(roomId, userId);
  touchRoomActivity(roomId, "http_take_shot");
  console.log("[sinuca-shoot-http-applied]", JSON.stringify({
    roomId,
    userId,
    shotSequence: game.shotSequence,
    turnUserId: game.turnUserId,
    phase: game.phase,
    foulReason: game.foulReason,
    cueBall: game.balls.find((ball) => ball.number === 0) ?? null,
  }));
  broadcastGame(roomId);
  res.json({ game });
}

app.get("/rooms", handleListRoomsHttp);
app.get("/api/rooms", handleListRoomsHttp);
app.post("/rooms", handleListRoomsHttp);
app.post("/api/rooms", handleListRoomsHttp);
app.get("/rooms/:roomId", handleGetRoomHttp);
app.get("/api/rooms/:roomId", handleGetRoomHttp);
app.get("/games/shoot", handleShootGameHttp);
app.get("/api/games/shoot", handleShootGameHttp);
app.get("/games/aim", handleGetAimHttp);
app.get("/api/games/aim", handleGetAimHttp);
app.get("/games/:roomId/aim", handleGetAimHttp);
app.get("/api/games/:roomId/aim", handleGetAimHttp);
app.get("/games/:roomId", handleGetGameHttp);
app.get("/api/games/:roomId", handleGetGameHttp);
app.get("/game/:roomId", handleGetGameHttp);
app.get("/api/game/:roomId", handleGetGameHttp);
app.get("/rooms/:roomId/game", handleGetGameHttp);
app.get("/api/rooms/:roomId/game", handleGetGameHttp);
app.get("/rooms/create", handleCreateRoomHttp);
app.post("/rooms/create", handleCreateRoomHttp);
app.get("/api/rooms/create", handleCreateRoomHttp);
app.post("/api/rooms/create", handleCreateRoomHttp);
app.post("/games/start", handleStartGameHttp);
app.post("/api/games/start", handleStartGameHttp);
app.post("/games/aim", handleSyncAimHttp);
app.post("/api/games/aim", handleSyncAimHttp);
app.post("/games/shoot", handleShootGameHttp);
app.post("/api/games/shoot", handleShootGameHttp);
app.get("/rooms/join", handleJoinRoomHttp);
app.post("/rooms/join", handleJoinRoomHttp);
app.get("/api/rooms/join", handleJoinRoomHttp);
app.post("/api/rooms/join", handleJoinRoomHttp);
app.get("/rooms/leave", handleLeaveRoomHttp);
app.post("/rooms/leave", handleLeaveRoomHttp);
app.get("/api/rooms/leave", handleLeaveRoomHttp);
app.post("/api/rooms/leave", handleLeaveRoomHttp);
app.get("/rooms/ready", handleReadyRoomHttp);
app.post("/rooms/ready", handleReadyRoomHttp);
app.get("/api/rooms/ready", handleReadyRoomHttp);
app.post("/api/rooms/ready", handleReadyRoomHttp);
app.get("/rooms/stake", handleUpdateStakeRoomHttp);
app.post("/rooms/stake", handleUpdateStakeRoomHttp);
app.get("/api/rooms/stake", handleUpdateStakeRoomHttp);
app.post("/api/rooms/stake", handleUpdateStakeRoomHttp);

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

function firstString(value: unknown): string | null {
  if (Array.isArray(value)) {
    return firstString(value[0]);
  }
  return typeof value === "string" ? value : null;
}


function booleanish(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return fallback;
    if (['true', '1', 'yes', 'on'].includes(normalized)) return true;
    if (['false', '0', 'no', 'off'].includes(normalized)) return false;
  }
  return fallback;
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



function toMongoLong(value: string | null | undefined): Long | null {
  const normalized = normalizeIntString(value);
  if (!normalized || !/^\d+$/.test(normalized)) return null;
  try {
    return Long.fromString(normalized, true);
  } catch {
    return null;
  }
}

function buildBalanceQuery(guildId: string, userId: string): { mongo: { type: string; guild_id: Long | string; user_id: Long | string }; debug: Record<string, string | null> } {
  const normalizedGuildId = normalizeIntString(guildId);
  const normalizedUserId = normalizeIntString(userId);
  const guildLong = toMongoLong(normalizedGuildId);
  const userLong = toMongoLong(normalizedUserId);

  return {
    mongo: {
      type: "user",
      guild_id: guildLong ?? normalizedGuildId ?? "",
      user_id: userLong ?? normalizedUserId ?? "",
    },
    debug: {
      type: "user",
      guild_id: normalizedGuildId,
      user_id: normalizedUserId,
    },
  };
}
interface BalanceLookupResult {
  balance: BalanceSnapshot;
  debug: BalanceDebugSnapshot;
}

async function fetchBalance(guildId: string, userId: string, session?: SessionContextPayload): Promise<BalanceLookupResult> {
  const coll = await ensureMongo();
  const querySpec = buildBalanceQuery(guildId, userId);
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
        query: querySpec.debug,
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

  let doc = await coll.findOne(querySpec.mongo, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
  let querySource = querySpec.mongo.guild_id instanceof Long || querySpec.mongo.user_id instanceof Long ? "mongo_long" : "mongo_string";

  if (!doc) {
    const stringQuery = { type: "user", guild_id: querySpec.debug.guild_id ?? "", user_id: querySpec.debug.user_id ?? "" };
    if (stringQuery.guild_id && stringQuery.user_id) {
      doc = await coll.findOne(stringQuery, { projection: { chips: 1, bonus_chips: 1, guild_id: 1, user_id: 1, type: 1 } });
      if (doc) querySource = "mongo_string";
    }
  }

  const chips = Number(doc?.chips ?? 0);
  const bonusChips = Number(doc?.bonus_chips ?? 0);
  const balance = {
    chips: Number.isFinite(chips) ? chips : 0,
    bonusChips: Number.isFinite(bonusChips) ? bonusChips : 0,
  };
  const debug: BalanceDebugSnapshot = {
    source: doc ? querySource : "mongo_default",
    sessionUserId: session?.userId ?? null,
    sessionGuildId: session?.guildId ?? null,
    requestUserId: userId,
    requestGuildId: guildId,
    mongoConnected: true,
    mongoDbName,
    mongoCollectionName,
    query: querySpec.debug,
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
  const action = firstString(req.body?.action) ?? firstString(req.query?.action);
  if (action === "rooms_list") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleListRoomsHttp(req, res);
  }
  if (action === "room_get") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleGetRoomHttp(req, res);
  }
  if (action === "room_create") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleCreateRoomHttp(req, res);
  }
  if (action === "room_join") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleJoinRoomHttp(req, res);
  }
  if (action === "room_leave") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleLeaveRoomHttp(req, res);
  }
  if (action === "room_ready") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleReadyRoomHttp(req, res);
  }
  if (action === "room_stake") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleUpdateStakeRoomHttp(req, res);
  }
  if (action === "game_get") {
    return void handleGetGameHttp(req, res);
  }
  if (action === "game_aim_get") {
    return void handleGetAimHttp(req, res);
  }
  if (action === "game_start") {
    console.log("[sinuca-balance-action]", JSON.stringify({ action, method: req.method, url: req.url ?? null }));
    return void handleStartGameHttp(req, res);
  }
  if (action === "game_aim_sync") {
    return void handleSyncAimHttp(req, res);
  }
  if (action === "game_shoot") {
    return void handleShootGameHttp(req, res);
  }
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
      query: { type: "user", guild_id: guildId ?? null, user_id: userId ?? null },
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
      query: { type: "user", guild_id: guildId, user_id: userId },
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
        query: { type: "user", guild_id: guildId, user_id: userId },
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

const roomIdleTicker = setInterval(() => {
  const now = Date.now();
  for (const [roomId, lastActivity] of roomActivityAt.entries()) {
    if (now - lastActivity < ROOM_IDLE_TIMEOUT_MS) continue;
    const room = getRoom(roomId);
    if (!room) {
      clearRoomActivity(roomId);
      continue;
    }
    console.log("[sinuca-room-idle-close]", JSON.stringify({
      roomId,
      idleMs: now - lastActivity,
      status: room.status,
      players: room.players.length,
    }));
    closeRoomAndNotify(roomId, "idle_timeout", "A sala foi fechada por 5 minutos de inatividade.");
  }
}, 15000);

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
      const nextRooms = listRooms(merged).map(toSnapshot);
      console.log("[sinuca-list-rooms]", JSON.stringify({ activeSession, request: data.payload, merged, rooms: nextRooms.map((room) => ({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, players: room.players.length, status: room.status, tableType: room.tableType, stakeChips: room.stakeChips })) }));
      watchContext(ws, merged);
      send(ws, { type: "room_list", payload: nextRooms });
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
      console.log("[sinuca-create-room-request]", JSON.stringify({ activeSession, request: data.payload, merged }));
      if (!instanceId || !userId || !displayName) {
        send(ws, { type: "error", message: "sessão da activity incompleta" });
        return;
      }
      const room = createRoom(instanceId, guildId ?? null, channelId ?? null, userId, displayName, merged.avatarUrl ?? null, { tableType: merged.tableType ?? null, stakeChips: merged.stakeChips ?? null });
      console.log("[sinuca-create-room-result]", JSON.stringify({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, tableType: room.tableType, stakeChips: room.stakeChips, players: room.players.length, status: room.status }));
      touchRoomActivity(room.roomId, "ws_create_room");
      subscribeSocket(room.roomId, ws);
      broadcastRoom(room.roomId);
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      return;
    }

    if (data.type === "join_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      if (!merged.userId || !merged.displayName) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = addPlayer(merged.roomId, merged.userId, merged.displayName, merged.avatarUrl ?? null);
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      touchRoomActivity(room.roomId, "ws_join_room");
      subscribeSocket(room.roomId, ws);
      broadcastRoom(room.roomId);
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      return;
    }

    if (data.type === "subscribe_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      const room = getRoom(merged.roomId);
      console.log("[sinuca-subscribe-room]", JSON.stringify({ activeSession, request: data.payload, merged, roomFound: Boolean(room) }));
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      subscribeSocket(room.roomId, ws);
      send(ws, { type: "room_state", payload: toSnapshot(room) });
      return;
    }

    if (data.type === "leave_room") {
      const merged = mergeWithSession(data.payload, activeSession);
      const previous = getRoom(merged.roomId);
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const shouldCloseRoom = booleanish(merged.closeRoom, false);
      if (previous?.status === "in_game") {
        removeGame(merged.roomId);
        setRoomInGame(merged.roomId, false);
      }
      const room = shouldCloseRoom && previous && previous.hostUserId === merged.userId
        ? null
        : removePlayer(merged.roomId, merged.userId);
      const closedRoom = shouldCloseRoom && previous && previous.hostUserId === merged.userId
        ? closeRoomAndNotify(merged.roomId, "host_closed_room", "A sala foi fechada pelo anfitrião.")
        : null;
      unsubscribeSocket(ws);
      if (room) {
        touchRoomActivity(room.roomId, "ws_leave_room_remaining");
        broadcastRoom(room.roomId);
        broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      } else if (!closedRoom && previous) {
        clearRoomActivity(previous.roomId);
        latestAimByRoom.delete(previous.roomId);
        aimRevisionByRoom.delete(previous.roomId);
        broadcastRoomList({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode });
      }
      return;
    }

    if (data.type === "start_game") {
      const merged = mergeWithSession(data.payload, activeSession);
      const room = getRoom(merged.roomId);
      if (!merged.userId || !room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      if (room.hostUserId !== merged.userId) {
        send(ws, { type: "error", message: "apenas o anfitrião pode iniciar" });
        return;
      }
      const opponent = room.players.find((player) => player.userId !== room.hostUserId);
      if (!opponent || !opponent.ready) {
        send(ws, { type: "error", message: "o adversário ainda não está pronto" });
        return;
      }
      setRoomInGame(room.roomId, true);
      startGameForRoom(room);
      latestAimByRoom.delete(room.roomId);
      touchRoomActivity(room.roomId, "ws_start_game");
      broadcastRoom(room.roomId);
      broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      broadcastGame(room.roomId);
      return;
    }

    if (data.type === "sync_aim") {
      const merged = mergeWithSession(data.payload, activeSession);
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = getRoom(merged.roomId);
      const game = getGameSnapshot(merged.roomId);
      if (!room || !game) return;

      const visible = Boolean(merged.visible);
      if (visible && game.turnUserId !== merged.userId) return;

      const payload = buildAimPayload({
        roomId: merged.roomId,
        userId: merged.userId,
        visible,
        angle: merged.angle,
        cueX: merged.cueX,
        cueY: merged.cueY,
        power: merged.power,
        seq: merged.seq,
        mode: merged.mode,
      });

      storeAimState(merged.roomId, payload);
      touchRoomActivity(merged.roomId, "ws_sync_aim");
      broadcastAim(merged.roomId, payload, ws);
      return;
    }

    if (data.type === "take_shot") {
      const merged = mergeWithSession(data.payload, activeSession);
      console.log("[sinuca-shoot-ws]", JSON.stringify({
        roomId: merged.roomId,
        userId: merged.userId ?? null,
        angle: Number(merged.angle ?? 0),
        power: Number(merged.power ?? 0),
        cueX: merged.cueX === undefined ? null : Number(merged.cueX),
        cueY: merged.cueY === undefined ? null : Number(merged.cueY),
        calledPocket: merged.calledPocket === undefined ? null : Number(merged.calledPocket),
        spinX: merged.spinX === undefined ? 0 : Number(merged.spinX),
        spinY: merged.spinY === undefined ? 0 : Number(merged.spinY),
      }));
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const game = getGameSnapshot(merged.roomId);
      if (!game) {
        send(ws, { type: "error", message: "partida não encontrada" });
        return;
      }
      if (game.turnUserId !== merged.userId) {
        send(ws, { type: "error", message: "não é sua vez" });
        return;
      }
      const applied = takeShot(
        merged.roomId,
        merged.userId,
        Number(merged.angle ?? 0),
        Number(merged.power ?? 0),
        merged.cueX === undefined ? null : Number(merged.cueX),
        merged.cueY === undefined ? null : Number(merged.cueY),
        merged.calledPocket === undefined ? null : Number(merged.calledPocket),
        merged.spinX === undefined ? 0 : Number(merged.spinX),
        merged.spinY === undefined ? 0 : Number(merged.spinY),
      );
      clearAimState(merged.roomId, merged.userId);
      touchRoomActivity(merged.roomId, "ws_take_shot");
      console.log("[sinuca-shoot-ws-applied]", JSON.stringify({
        roomId: merged.roomId,
        shotSequence: applied?.shotSequence ?? null,
        turnUserId: applied?.turnUserId ?? null,
        phase: applied?.phase ?? null,
      }));
      broadcastGame(merged.roomId);
      return;
    }

    if (data.type === "set_ready") {
      const merged = mergeWithSession(data.payload, activeSession);
      if (!merged.userId) {
        send(ws, { type: "error", message: "jogador da activity não identificado" });
        return;
      }
      const room = setPlayerReady(merged.roomId, merged.userId, booleanish(merged.ready, false));
      if (!room) {
        send(ws, { type: "error", message: "mesa não encontrada" });
        return;
      }
      touchRoomActivity(room.roomId, "ws_set_ready");
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
