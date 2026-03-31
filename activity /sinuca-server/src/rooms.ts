import type { WebSocket } from "ws";
import type { ListRoomsPayload, RoomSnapshot, RoomStatus, TableType } from "./messages.js";

interface PlayerRef {
  userId: string;
  displayName: string;
  ready: boolean;
  avatarUrl?: string | null;
}

interface RoomRecord {
  roomId: string;
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: "server" | "casual";
  tableType: TableType;
  stakeChips: number | null;
  hostUserId: string;
  hostDisplayName: string;
  players: PlayerRef[];
  status: RoomStatus;
  stakeLabel: string;
  createdAt: number;
}

const rooms = new Map<string, RoomRecord>();
const roomSockets = new Map<string, Set<WebSocket>>();
const socketRoom = new Map<WebSocket, string>();

function makeRoomId(mode: "server" | "casual") {
  const head = mode === "server" ? "mesa" : "casual";
  return `${head}-${Math.random().toString(36).slice(2, 8)}`;
}

function computeStatus(room: RoomRecord): RoomStatus {
  if (room.status === "in_game") return room.status;
  if (room.players.length >= 2 && room.players.every((player) => player.ready)) return "ready";
  return "waiting";
}

function sameContext(room: RoomRecord, payload: ListRoomsPayload) {
  if (room.mode !== payload.mode) return false;
  if (payload.mode === "casual") return true;
  return room.guildId === (payload.guildId ?? null) && room.channelId === (payload.channelId ?? null);
}

export function createRoom(
  instanceId: string,
  guildId: string | null | undefined,
  channelId: string | null | undefined,
  userId: string,
  displayName: string,
  avatarUrl?: string | null,
  options?: { tableType?: TableType | null; stakeChips?: number | null },
): RoomRecord {
  const mode = guildId ? "server" : "casual";
  const allowedStake = new Set([10, 25, 50]);
  const requestedTableType = options?.tableType === "casual" ? "casual" : "stake";
  const tableType: TableType = mode === "server" ? requestedTableType : "casual";
  const normalizedStake = tableType === "stake" && allowedStake.has(Number(options?.stakeChips)) ? Number(options?.stakeChips) : 25;
  const room: RoomRecord = {
    roomId: makeRoomId(mode),
    instanceId,
    guildId: guildId ?? null,
    channelId: channelId ?? null,
    mode,
    tableType,
    stakeChips: tableType === "stake" ? normalizedStake : null,
    hostUserId: userId,
    hostDisplayName: displayName,
    players: [{ userId, displayName, ready: false, avatarUrl: avatarUrl ?? null }],
    status: "waiting",
    stakeLabel: tableType === "stake" ? `${normalizedStake} fichas` : "casual",
    createdAt: Date.now(),
  };
  rooms.set(room.roomId, room);
  roomSockets.set(room.roomId, new Set());
  return room;
}

export function listRooms(payload: ListRoomsPayload): RoomRecord[] {
  return [...rooms.values()]
    .filter((room) => sameContext(room, payload))
    .sort((a, b) => b.createdAt - a.createdAt);
}

export function getRoom(roomId: string): RoomRecord | null {
  return rooms.get(roomId) ?? null;
}

export function addPlayer(roomId: string, userId: string, displayName: string, avatarUrl?: string | null): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  const existing = room.players.find((player) => player.userId === userId);
  if (existing) {
    existing.displayName = displayName;
    existing.avatarUrl = avatarUrl ?? existing.avatarUrl ?? null;
  } else {
    if (room.players.length >= 2) return room;
    room.players.push({ userId, displayName, ready: false, avatarUrl: avatarUrl ?? null });
  }
  room.status = computeStatus(room);
  return room;
}

export function removePlayer(roomId: string, userId: string): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  room.players = room.players.filter((player) => player.userId !== userId);
  if (room.players.length === 0) {
    rooms.delete(roomId);
    roomSockets.delete(roomId);
    return null;
  }
  if (room.hostUserId === userId) {
    room.hostUserId = room.players[0].userId;
    room.hostDisplayName = room.players[0].displayName;
  }
  room.status = computeStatus(room);
  return room;
}

export function setPlayerReady(roomId: string, userId: string, ready: boolean): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  const player = room.players.find((entry) => entry.userId === userId);
  if (!player) return room;
  player.ready = ready;
  room.status = computeStatus(room);
  return room;
}

export function subscribeSocket(roomId: string, ws: WebSocket): Set<WebSocket> {
  const previousRoom = socketRoom.get(ws);
  if (previousRoom && previousRoom !== roomId) {
    roomSockets.get(previousRoom)?.delete(ws);
  }
  const bucket = roomSockets.get(roomId) ?? new Set<WebSocket>();
  bucket.add(ws);
  roomSockets.set(roomId, bucket);
  socketRoom.set(ws, roomId);
  return bucket;
}

export function getSubscribers(roomId: string): Set<WebSocket> {
  return roomSockets.get(roomId) ?? new Set<WebSocket>();
}

export function unsubscribeSocket(ws: WebSocket) {
  const roomId = socketRoom.get(ws);
  if (roomId) {
    roomSockets.get(roomId)?.delete(ws);
    socketRoom.delete(ws);
  }
}

export function toSnapshot(room: RoomRecord): RoomSnapshot {
  return {
    roomId: room.roomId,
    instanceId: room.instanceId,
    guildId: room.guildId,
    channelId: room.channelId,
    mode: room.mode,
    tableType: room.tableType,
    stakeChips: room.stakeChips,
    hostUserId: room.hostUserId,
    hostDisplayName: room.hostDisplayName,
    players: room.players,
    status: room.status,
    stakeLabel: room.stakeLabel,
    createdAt: room.createdAt,
  };
}
