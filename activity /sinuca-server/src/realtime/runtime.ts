import type { WebSocket } from "ws";
import {
  closeRoom,
  getRoom,
  getSubscribers,
  listRooms,
  toSnapshot,
} from "../rooms.js";
import type { RoomRecord } from "../rooms.js";
import {
  getGameSnapshot,
  removeGame,
  stepRealtimeGames,
} from "../gameState.js";
import type {
  AimPointerMode,
  AimStateSnapshot,
  ListRoomsPayload,
  ServerMessage,
} from "../messages.js";
import { ROOM_CLOSE_REASONS } from "../shared/contracts.js";

const ROOM_IDLE_TIMEOUT_MS = 5 * 60 * 1000;

function send(ws: WebSocket, payload: ServerMessage) {
  ws.send(JSON.stringify(payload));
}

function normalizeAimMode(value: unknown): AimPointerMode {
  return value === "aim" || value === "place" || value === "power" || value === "idle" ? value : "idle";
}

function contextKey(payload: ListRoomsPayload) {
  if (payload.mode === "server") {
    return `${payload.mode}:${payload.guildId ?? ""}`;
  }
  return `${payload.mode}:${payload.guildId ?? ""}:${payload.channelId ?? ""}`;
}

export interface ActivityRealtimeRuntime {
  send: typeof send;
  touchRoomActivity(roomId: string, source: string): void;
  clearRoomActivity(roomId: string): void;
  closeRoomAndNotify(roomId: string, reason: string, message: string): RoomRecord | null;
  watchContext(ws: WebSocket, payload: ListRoomsPayload): void;
  unwatchContext(ws: WebSocket): void;
  broadcastRoom(roomId: string): void;
  broadcastRoomList(payload: ListRoomsPayload): void;
  broadcastGame(roomId: string): void;
  broadcastAim(roomId: string, payload: AimStateSnapshot, except?: WebSocket | null): void;
  storeAimState(roomId: string, payload: AimStateSnapshot): void;
  clearAimState(roomId: string, userId?: string | null): void;
  dropAimState(roomId: string): void;
  getAimState(roomId: string): AimStateSnapshot | null;
  buildAimPayload(input: {
    roomId: string;
    userId: string;
    visible: boolean;
    angle?: unknown;
    cueX?: unknown;
    cueY?: unknown;
    power?: unknown;
    seq?: unknown;
    mode?: unknown;
  }): AimStateSnapshot;
  startLifecycle(): () => void;
}

export function createActivityRealtimeRuntime(): ActivityRealtimeRuntime {
  const contextWatchers = new Map<string, Set<WebSocket>>();
  const socketContext = new Map<WebSocket, string>();
  const latestAimByRoom = new Map<string, AimStateSnapshot>();
  const aimRevisionByRoom = new Map<string, number>();
  const roomActivityAt = new Map<string, number>();
  const pendingRealtimeBroadcastRooms = new Set<string>();
  const realtimeDebugByRoom = new Map<string, { lastLogAt: number; lastStepAt: number; lastBroadcastAt: number; stepCount: number; broadcastCount: number; maxStepGapMs: number; maxBroadcastGapMs: number; }>();

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


  function dropAimState(roomId: string) {
    latestAimByRoom.delete(roomId);
    aimRevisionByRoom.delete(roomId);
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
  }): AimStateSnapshot {
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
      console.log("[sinuca-realtime-debug]", JSON.stringify({ roomId, status, revision, stepsPerWindow: current.stepCount, broadcastsPerWindow: current.broadcastCount, maxStepGapMs: current.maxStepGapMs, maxBroadcastGapMs: current.maxBroadcastGapMs, pendingRooms: pendingRealtimeBroadcastRooms.size }));
      current.lastLogAt = now;
      current.stepCount = 0;
      current.broadcastCount = 0;
      current.maxStepGapMs = 0;
      current.maxBroadcastGapMs = 0;
    }
    realtimeDebugByRoom.set(roomId, current);
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

  function broadcastAim(roomId: string, payload: AimStateSnapshot, except?: WebSocket | null) {
    const message: ServerMessage = { type: "aim_state", payload };
    for (const client of getSubscribers(roomId)) {
      if (except && client === except) continue;
      send(client, message);
    }
  }

  function getAimState(roomId: string): AimStateSnapshot | null {
    return latestAimByRoom.get(roomId) ?? null;
  }

  function startLifecycle() {
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
        closeRoomAndNotify(roomId, ROOM_CLOSE_REASONS.idleTimeout, "A sala foi fechada por 5 minutos de inatividade.");
      }
    }, 15000);
    roomIdleTicker.unref?.();

    return () => {
      clearInterval(realtimeStepInterval);
      clearInterval(realtimeBroadcastInterval);
      clearInterval(roomIdleTicker);
    };
  }

  return {
    send,
    touchRoomActivity,
    clearRoomActivity,
    closeRoomAndNotify,
    watchContext,
    unwatchContext,
    broadcastRoom,
    broadcastRoomList,
    broadcastGame,
    broadcastAim,
    storeAimState,
    clearAimState,
    dropAimState,
    getAimState,
    buildAimPayload,
    startLifecycle,
  };
}
