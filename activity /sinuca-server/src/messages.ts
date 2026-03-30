export type RoomMode = "server" | "casual";

export interface CreateRoomPayload {
  instanceId: string;
  guildId?: string | null;
  channelId?: string | null;
}

export interface JoinRoomPayload {
  instanceId: string;
  userId: string;
  displayName: string;
}

export type ClientMessage =
  | { type: "create_room"; payload: CreateRoomPayload }
  | { type: "join_room"; payload: JoinRoomPayload }
  | { type: "ping" };

export interface RoomSnapshot {
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: RoomMode;
  players: Array<{ userId: string; displayName: string }>;
  createdAt: number;
}

export type ServerMessage =
  | { type: "ready" }
  | { type: "pong" }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "error"; message: string };
