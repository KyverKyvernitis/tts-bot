export type RoomMode = "server" | "casual";
export type RoomStatus = "waiting" | "ready" | "in_game";

export interface ContextPayload {
  guildId?: string | null;
  channelId?: string | null;
  mode: RoomMode;
}

export interface CreateRoomPayload extends ContextPayload {
  instanceId: string;
  userId: string;
  displayName: string;
}

export interface JoinRoomPayload {
  roomId: string;
  userId: string;
  displayName: string;
}

export interface LeaveRoomPayload {
  roomId: string;
  userId: string;
}

export interface ReadyPayload {
  roomId: string;
  userId: string;
  ready: boolean;
}

export interface ListRoomsPayload extends ContextPayload {}

export interface BalancePayload {
  guildId: string | null;
  userId: string | null;
}

export interface SessionContextPayload {
  userId: string | null;
  displayName: string | null;
  guildId: string | null;
  channelId: string | null;
  instanceId: string | null;
}

export type ClientMessage =
  | { type: "create_room"; payload: CreateRoomPayload }
  | { type: "join_room"; payload: JoinRoomPayload }
  | { type: "leave_room"; payload: LeaveRoomPayload }
  | { type: "set_ready"; payload: ReadyPayload }
  | { type: "list_rooms"; payload: ListRoomsPayload }
  | { type: "get_balance"; payload: BalancePayload }
  | { type: "init_context"; payload: SessionContextPayload }
  | { type: "ping" };

export interface RoomSnapshot {
  roomId: string;
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: RoomMode;
  hostUserId: string;
  hostDisplayName: string;
  players: Array<{ userId: string; displayName: string; ready: boolean }>;
  status: RoomStatus;
  stakeLabel: string;
  createdAt: number;
}

export interface BalanceSnapshot {
  chips: number;
  bonusChips: number;
}

export interface BalanceDebugSnapshot {
  source: string;
  sessionUserId: string | null;
  sessionGuildId: string | null;
  requestUserId: string | null;
  requestGuildId: string | null;
  mongoConnected: boolean;
  mongoDbName: string;
  mongoCollectionName: string;
  query: Record<string, number | string | null>;
  docFound: boolean;
  docKeys: string[];
  rawChips: unknown;
  rawBonusChips: unknown;
  normalizedChips: number;
  normalizedBonusChips: number;
  note: string;
}

export type ServerMessage =
  | { type: "ready" }
  | { type: "session_context"; payload: SessionContextPayload }
  | { type: "pong" }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] }
  | { type: "balance_state"; payload: BalanceSnapshot }
  | { type: "balance_debug"; payload: BalanceDebugSnapshot }
  | { type: "error"; message: string };
