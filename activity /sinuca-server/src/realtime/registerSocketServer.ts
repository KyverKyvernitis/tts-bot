import type { IncomingMessage } from "http";
import type { WebSocketServer, WebSocket } from "ws";
import {
  addPlayer,
  createRoom,
  getRoom,
  removePlayer,
  setPlayerReady,
  setRoomInGame,
  subscribeSocket,
  toSnapshot,
  unsubscribeSocket,
  listRooms,
} from "../rooms.js";
import {
  getGameSnapshot,
  removeGame,
  startGameForRoom,
  takeShot,
} from "../gameState.js";
import type {
  ClientMessage,
  SessionContextPayload,
} from "../messages.js";
import type { BalanceService } from "../services/balanceService.js";
import type { ActivityRealtimeRuntime } from "./runtime.js";
import {
  booleanish,
  mergeSession,
  mergeWithSession,
  resolveRequestSession,
} from "../shared/session.js";
import { ROOM_CLOSE_REASONS } from "../shared/contracts.js";

export interface RegisterSocketServerOptions {
  wss: WebSocketServer;
  runtime: ActivityRealtimeRuntime;
  balanceService: BalanceService;
  exchangeDiscordCode(code: string): Promise<{ ok: boolean; accessToken: string | null; error: string | null; detail: string | null }>;
}

export function registerSocketServer({ wss, runtime, balanceService, exchangeDiscordCode }: RegisterSocketServerOptions) {
  const socketSession = new Map<WebSocket, SessionContextPayload>();
  const balanceWatchers = new Map<WebSocket, { guildId: string; userId: string; lastSent: string }>();

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
      const result = await balanceService.fetchBalance(guildId, userId, session);
      const nextKey = JSON.stringify(result.balance);
      const current = balanceWatchers.get(ws);
      if (!current) return;
      if (!force && current.lastSent === nextKey) return;
      current.lastSent = nextKey;
      console.log("[sinuca-balance-push]", JSON.stringify({ guildId, userId, force, nextKey, source: result.debug.source, docFound: result.debug.docFound }));
      runtime.send(ws, { type: "balance_state", payload: result.balance });
      runtime.send(ws, { type: "balance_debug", payload: result.debug });
    } catch (error) {
      console.error("[sinuca-balance-error]", error);
      if (force) {
        runtime.send(ws, { type: "balance_state", payload: { chips: 0, bonusChips: 0 } });
        runtime.send(ws, {
          type: "balance_debug",
          payload: balanceService.buildErrorDebug({
            session,
            guildId,
            userId,
            note: "erro ao buscar saldo",
          }),
        });
      }
    }
  }

  const balanceTicker = setInterval(() => {
    for (const [ws, watch] of balanceWatchers.entries()) {
      if (ws.readyState !== 1) continue;
      void pushBalance(ws, watch.guildId, watch.userId);
    }
  }, 2000);
  balanceTicker.unref?.();

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
    runtime.send(ws, { type: "ready" });
    runtime.send(ws, { type: "session_context", payload: session });
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
        runtime.send(ws, { type: "error", message: "payload inválido" });
        return;
      }

      const activeSession = socketSession.get(ws) ?? session;

      if (data.type === "ping") {
        runtime.send(ws, { type: "pong" });
        return;
      }

      if (data.type === "exchange_token") {
        const code = typeof data.payload?.code === "string" ? data.payload.code : "";
        const result = await exchangeDiscordCode(code);
        runtime.send(ws, {
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
        runtime.send(ws, { type: "session_context", payload: nextSession });
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
        runtime.watchContext(ws, merged);
        runtime.send(ws, { type: "room_list", payload: nextRooms });
        return;
      }

      if (data.type === "get_balance") {
        const merged = mergeWithSession(data.payload, activeSession);
        console.log("[sinuca-get-balance]", JSON.stringify({ activeSession, request: data.payload, merged }));
        if (!merged.guildId || !merged.userId) {
          runtime.send(ws, { type: "balance_state", payload: { chips: 0, bonusChips: 0 } });
          runtime.send(ws, {
            type: "balance_debug",
            payload: balanceService.buildMissingIdentifiersDebug({
              session: activeSession,
              guildId: merged.guildId ?? null,
              userId: merged.userId ?? null,
              note: "guildId ou userId ausente na sessão/request",
            }),
          });
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
          runtime.send(ws, { type: "error", message: "sessão da activity incompleta" });
          return;
        }
        const room = createRoom(instanceId, guildId ?? null, channelId ?? null, userId, displayName, merged.avatarUrl ?? null, { tableType: merged.tableType ?? null, stakeChips: merged.stakeChips ?? null });
        console.log("[sinuca-create-room-result]", JSON.stringify({ roomId: room.roomId, guildId: room.guildId, channelId: room.channelId, mode: room.mode, tableType: room.tableType, stakeChips: room.stakeChips, players: room.players.length, status: room.status }));
        runtime.touchRoomActivity(room.roomId, "ws_create_room");
        subscribeSocket(room.roomId, ws);
        runtime.broadcastRoom(room.roomId);
        runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
        return;
      }

      if (data.type === "join_room") {
        const merged = mergeWithSession(data.payload, activeSession);
        if (!merged.userId || !merged.displayName) {
          runtime.send(ws, { type: "error", message: "jogador da activity não identificado" });
          return;
        }
        const room = addPlayer(merged.roomId, merged.userId, merged.displayName, merged.avatarUrl ?? null);
        if (!room) {
          runtime.send(ws, { type: "error", message: "mesa não encontrada" });
          return;
        }
        runtime.touchRoomActivity(room.roomId, "ws_join_room");
        subscribeSocket(room.roomId, ws);
        runtime.broadcastRoom(room.roomId);
        runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
        return;
      }

      if (data.type === "subscribe_room") {
        const merged = mergeWithSession(data.payload, activeSession);
        const room = getRoom(merged.roomId);
        console.log("[sinuca-subscribe-room]", JSON.stringify({ activeSession, request: data.payload, merged, roomFound: Boolean(room) }));
        if (!room) {
          runtime.send(ws, { type: "error", message: "mesa não encontrada" });
          return;
        }
        subscribeSocket(room.roomId, ws);
        runtime.send(ws, { type: "room_state", payload: toSnapshot(room) });
        return;
      }

      if (data.type === "leave_room") {
        const merged = mergeWithSession(data.payload, activeSession);
        const previous = getRoom(merged.roomId);
        if (!merged.userId) {
          runtime.send(ws, { type: "error", message: "jogador da activity não identificado" });
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
          ? runtime.closeRoomAndNotify(merged.roomId, ROOM_CLOSE_REASONS.hostClosedRoom, "A sala foi fechada pelo anfitrião.")
          : null;
        unsubscribeSocket(ws);
        if (room) {
          runtime.touchRoomActivity(room.roomId, "ws_leave_room_remaining");
          runtime.broadcastRoom(room.roomId);
          runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
        } else if (!closedRoom && previous) {
          runtime.clearRoomActivity(previous.roomId);
          runtime.dropAimState(previous.roomId);
          runtime.broadcastRoomList({ guildId: previous.guildId, channelId: previous.channelId, mode: previous.mode });
        }
        return;
      }

      if (data.type === "start_game") {
        const merged = mergeWithSession(data.payload, activeSession);
        const room = getRoom(merged.roomId);
        if (!merged.userId || !room) {
          runtime.send(ws, { type: "error", message: "mesa não encontrada" });
          return;
        }
        if (room.hostUserId !== merged.userId) {
          runtime.send(ws, { type: "error", message: "apenas o anfitrião pode iniciar" });
          return;
        }
        const opponent = room.players.find((player) => player.userId !== room.hostUserId);
        if (!opponent || !opponent.ready) {
          runtime.send(ws, { type: "error", message: "o adversário ainda não está pronto" });
          return;
        }
        setRoomInGame(room.roomId, true);
        startGameForRoom(room);
        runtime.dropAimState(room.roomId);
        runtime.touchRoomActivity(room.roomId, "ws_start_game");
        runtime.broadcastRoom(room.roomId);
        runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
        runtime.broadcastGame(room.roomId);
        return;
      }

      if (data.type === "sync_aim") {
        const merged = mergeWithSession(data.payload, activeSession);
        if (!merged.userId) {
          runtime.send(ws, { type: "error", message: "jogador da activity não identificado" });
          return;
        }
        const room = getRoom(merged.roomId);
        const game = getGameSnapshot(merged.roomId);
        if (!room || !game) return;

        const visible = Boolean(merged.visible);
        if (visible && game.turnUserId !== merged.userId) return;

        const payload = runtime.buildAimPayload({
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

        runtime.storeAimState(merged.roomId, payload);
        runtime.touchRoomActivity(merged.roomId, "ws_sync_aim");
        runtime.broadcastAim(merged.roomId, payload, ws);
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
          runtime.send(ws, { type: "error", message: "jogador da activity não identificado" });
          return;
        }
        const game = getGameSnapshot(merged.roomId);
        if (!game) {
          runtime.send(ws, { type: "error", message: "partida não encontrada" });
          return;
        }
        if (game.turnUserId !== merged.userId) {
          runtime.send(ws, { type: "error", message: "não é sua vez" });
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
        const clearedAim = runtime.clearAimState(merged.roomId, merged.userId);
        if (clearedAim) {
          runtime.broadcastAim(merged.roomId, clearedAim);
        }
        runtime.touchRoomActivity(merged.roomId, "ws_take_shot");
        console.log("[sinuca-shoot-ws-applied]", JSON.stringify({
          roomId: merged.roomId,
          shotSequence: applied?.shotSequence ?? null,
          turnUserId: applied?.turnUserId ?? null,
          phase: applied?.phase ?? null,
        }));
        runtime.broadcastGame(merged.roomId);
        return;
      }

      if (data.type === "set_ready") {
        const merged = mergeWithSession(data.payload, activeSession);
        if (!merged.userId) {
          runtime.send(ws, { type: "error", message: "jogador da activity não identificado" });
          return;
        }
        const room = setPlayerReady(merged.roomId, merged.userId, booleanish(merged.ready, false));
        if (!room) {
          runtime.send(ws, { type: "error", message: "mesa não encontrada" });
          return;
        }
        runtime.touchRoomActivity(room.roomId, "ws_set_ready");
        runtime.broadcastRoom(room.roomId);
        runtime.broadcastRoomList({ guildId: room.guildId, channelId: room.channelId, mode: room.mode });
      }
    });

    ws.on("close", () => {
      unsubscribeSocket(ws);
      runtime.unwatchContext(ws);
      balanceWatchers.delete(ws);
      socketSession.delete(ws);
    });
  });

  return () => {
    clearInterval(balanceTicker);
  };
}
