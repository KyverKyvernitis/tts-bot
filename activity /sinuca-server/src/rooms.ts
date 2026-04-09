import type { WebSocket } from "ws";
import type { ListRoomsPayload, RoomSnapshot, RoomStatus, TableType } from "./messages.js";

export type StakeGateAcceptanceKind = "ok" | "bonus" | "debt" | "negative";

export interface StakeGateAcceptance {
  kind: StakeGateAcceptanceKind;
  stakeChips: number;
  resultingChips: number;
  resultingBonusChips: number;
  bonusToUse: number;
  acceptedAt: number;
}

export interface PlayerRef {
  userId: string;
  displayName: string;
  ready: boolean;
  avatarUrl?: string | null;
  stakeGateAcceptance?: StakeGateAcceptance | null;
}

export interface RoomRecord {
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
  rematchReadyUserIds: string[];
}

const rooms = new Map<string, RoomRecord>();
const roomSockets = new Map<string, Set<WebSocket>>();
const socketRoom = new Map<WebSocket, string>();

function makeRoomId(mode: "server" | "casual") {
  const head = mode === "server" ? "mesa" : "casual";
  return `${head}-${Math.random().toString(36).slice(2, 8)}`;
}

function computeStatus(room: RoomRecord): RoomStatus {
  if (room.status === "in_game" && room.players.length >= 2) return "in_game";
  if (room.players.length >= 2 && room.players.every((player) => player.ready)) return "ready";
  return "waiting";
}

function sameContext(room: RoomRecord, payload: ListRoomsPayload) {
  if (room.mode !== payload.mode) return false;
  if (payload.mode === "casual") return true;
  return room.guildId === (payload.guildId ?? null);
}

function normalizeStake(tableType: TableType, stakeChips: number | null | undefined) {
  const allowedStake = new Set([10, 25, 30, 50]);
  const normalized = Number(stakeChips);
  if (tableType === "stake" && allowedStake.has(normalized)) return normalized;
  return 25;
}

function findExistingHostRoom(mode: "server" | "casual", guildId: string | null, channelId: string | null, userId: string) {
  for (const room of rooms.values()) {
    if (room.hostUserId !== userId) continue;
    if (room.mode !== mode) continue;
    if (mode === "server") {
      if (room.guildId === guildId) return room;
      continue;
    }
    if (room.channelId === channelId) return room;
  }
  return null;
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
  const requestedTableType = options?.tableType === "casual" ? "casual" : "stake";
  const tableType: TableType = mode === "server" ? requestedTableType : "casual";
  const normalizedStake = normalizeStake(tableType, options?.stakeChips ?? null);
  const existing = findExistingHostRoom(mode, guildId ?? null, channelId ?? null, userId);

  if (existing) {
    existing.instanceId = instanceId;
    existing.guildId = guildId ?? null;
    existing.channelId = channelId ?? null;
    existing.hostDisplayName = displayName;
    existing.tableType = tableType;
    existing.stakeChips = tableType === "stake" ? normalizedStake : null;
    existing.stakeLabel = tableType === "stake" ? `${normalizedStake} fichas` : "casual";
    const hostPlayer = existing.players.find((player) => player.userId === userId);
    if (hostPlayer) {
      hostPlayer.displayName = displayName;
      hostPlayer.avatarUrl = avatarUrl ?? hostPlayer.avatarUrl ?? null;
      hostPlayer.stakeGateAcceptance = null;
    }
    for (const player of existing.players) {
      player.stakeGateAcceptance = null;
    }
    existing.status = computeStatus(existing);
    return existing;
  }

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
    players: [{ userId, displayName, ready: false, avatarUrl: avatarUrl ?? null, stakeGateAcceptance: null }],
    status: "waiting",
    stakeLabel: tableType === "stake" ? `${normalizedStake} fichas` : "casual",
    createdAt: Date.now(),
    rematchReadyUserIds: [],
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

export function closeRoom(roomId: string): RoomRecord | null {
  const room = rooms.get(roomId) ?? null;
  if (!room) return null;
  rooms.delete(roomId);
  roomSockets.delete(roomId);
  return room;
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
    room.players.push({ userId, displayName, ready: false, avatarUrl: avatarUrl ?? null, stakeGateAcceptance: null });
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

export function setRoomStake(roomId: string, hostUserId: string, options?: { tableType?: TableType | null; stakeChips?: number | null }): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  if (room.hostUserId !== hostUserId) return room;

  const nextTableType: TableType = options?.tableType === "casual" || Number(options?.stakeChips ?? 0) === 0 ? "casual" : "stake";
  const nextStake = nextTableType === "stake" ? normalizeStake(nextTableType, options?.stakeChips ?? room.stakeChips) : null;

  room.tableType = nextTableType;
  room.stakeChips = nextStake;
  room.stakeLabel = nextTableType === "stake" ? `${nextStake} fichas` : "casual";

  for (const player of room.players) {
    if (player.userId === room.hostUserId) continue;
    player.ready = false;
  }
  for (const player of room.players) {
    player.stakeGateAcceptance = null;
  }

  room.status = computeStatus(room);
  return room;
}

export function setPlayerStakeGateAcceptance(roomId: string, userId: string, acceptance: StakeGateAcceptance | null): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  const player = room.players.find((entry) => entry.userId === userId);
  if (!player) return room;
  player.stakeGateAcceptance = acceptance;
  return room;
}

export function clearAllStakeGateAcceptances(roomId: string): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  for (const player of room.players) {
    player.stakeGateAcceptance = null;
  }
  return room;
}

export function setRoomInGame(roomId: string, inGame: boolean): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  room.status = inGame ? "in_game" : computeStatus({ ...room, status: "waiting" });
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

export function toggleRematchReady(roomId: string, userId: string): RoomRecord | null {
  const room = rooms.get(roomId);
  if (!room) return null;
  if (!room.players.some((p) => p.userId === userId)) return null;
  if (!room.rematchReadyUserIds) room.rematchReadyUserIds = [];
  const idx = room.rematchReadyUserIds.indexOf(userId);
  if (idx >= 0) {
    room.rematchReadyUserIds.splice(idx, 1);
  } else {
    room.rematchReadyUserIds.push(userId);
  }
  return room;
}

export function clearRematchReady(roomId: string): void {
  const room = rooms.get(roomId);
  if (room) room.rematchReadyUserIds = [];
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
    rematchReadyUserIds: room.rematchReadyUserIds?.length ? room.rematchReadyUserIds : undefined,
  };
}
