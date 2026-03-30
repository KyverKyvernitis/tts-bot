import type { WebSocket } from "ws";
import type { RoomSnapshot } from "./messages.js";

interface PlayerRef {
  userId: string;
  displayName: string;
}

interface RoomRecord {
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: "server" | "casual";
  createdAt: number;
  players: PlayerRef[];
}

const rooms = new Map<string, RoomRecord>();
const roomSockets = new Map<string, Set<WebSocket>>();

export function getOrCreateRoom(instanceId: string, guildId?: string | null, channelId?: string | null): RoomRecord {
  const found = rooms.get(instanceId);
  if (found) return found;

  const room: RoomRecord = {
    instanceId,
    guildId: guildId ?? null,
    channelId: channelId ?? null,
    mode: guildId ? "server" : "casual",
    createdAt: Date.now(),
    players: [],
  };
  rooms.set(instanceId, room);
  roomSockets.set(instanceId, new Set());
  return room;
}

export function addPlayer(instanceId: string, userId: string, displayName: string): RoomRecord | null {
  const room = rooms.get(instanceId);
  if (!room) return null;

  const exists = room.players.some((player) => player.userId === userId);
  if (!exists) {
    room.players.push({ userId, displayName });
  }
  return room;
}

export function subscribeSocket(instanceId: string, ws: WebSocket): Set<WebSocket> {
  const bucket = roomSockets.get(instanceId) ?? new Set<WebSocket>();
  bucket.add(ws);
  roomSockets.set(instanceId, bucket);
  return bucket;
}

export function unsubscribeSocket(ws: WebSocket) {
  for (const bucket of roomSockets.values()) {
    bucket.delete(ws);
  }
}

export function toSnapshot(room: RoomRecord): RoomSnapshot {
  return {
    instanceId: room.instanceId,
    guildId: room.guildId,
    channelId: room.channelId,
    mode: room.mode,
    players: room.players,
    createdAt: room.createdAt,
  };
}
