export type RoomMode = "server" | "casual";
export type TableType = "stake" | "casual";
export type RoomStatus = "waiting" | "ready" | "in_game";
export type GameStatus = "waiting_shot" | "finished";
export type BallGroup = "solids" | "stripes";
export type GamePhase = "break" | "open_table" | "group_play" | "eight_ball" | "finished";

export interface ContextPayload {
  guildId?: string | null;
  channelId?: string | null;
  mode: RoomMode;
}

export interface CreateRoomPayload extends ContextPayload {
  instanceId: string;
  userId: string;
  displayName: string;
  avatarUrl?: string | null;
  tableType?: TableType | null;
  stakeChips?: number | null;
}

export interface JoinRoomPayload {
  roomId: string;
  userId: string;
  displayName: string;
  avatarUrl?: string | null;
}

export interface LeaveRoomPayload {
  roomId: string;
  userId: string;
  closeRoom?: boolean;
}

export interface ReadyPayload {
  roomId: string;
  userId: string;
  ready: boolean;
}

export interface StartGamePayload {
  roomId: string;
  userId: string;
}

export interface ShootPayload {
  roomId: string;
  userId: string;
  angle: number;
  power: number;
  cueX?: number | null;
  cueY?: number | null;
  calledPocket?: number | null;
}

export interface GameBallSnapshot {
  id: string;
  number: number;
  x: number;
  y: number;
  pocketed: boolean;
}

export interface GameShotFrameBall {
  id: string;
  x: number;
  y: number;
  pocketed: boolean;
}

export interface GameShotFrame {
  balls: GameShotFrameBall[];
}

export interface GameShotSnapshot {
  seq: number;
  shooterUserId: string;
  nextTurnUserId: string;
  pocketedNumbers: number[];
  cuePocketed: boolean;
  frames: GameShotFrame[];
  createdAt: number;
}

export interface GameSnapshot {
  gameId: string;
  roomId: string;
  hostUserId: string;
  guestUserId: string | null;
  tableType: TableType;
  stakeChips: number | null;
  status: GameStatus;
  phase: GamePhase;
  turnUserId: string;
  shotSequence: number;
  hostGroup: BallGroup | null;
  guestGroup: BallGroup | null;
  ballInHandUserId: string | null;
  winnerUserId: string | null;
  foulReason: string | null;
  calledPocket: number | null;
  balls: GameBallSnapshot[];
  createdAt: number;
  updatedAt: number;
  lastShot: GameShotSnapshot | null;
}

export interface ListRoomsPayload extends ContextPayload {}

export interface BalancePayload {
  guildId: string | null;
  userId: string | null;
}

export interface ExchangeTokenPayload {
  code: string;
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
  | { type: "exchange_token"; payload: ExchangeTokenPayload }
  | { type: "start_game"; payload: StartGamePayload }
  | { type: "take_shot"; payload: ShootPayload }
  | { type: "ping" };

export interface RoomSnapshot {
  roomId: string;
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: RoomMode;
  tableType: TableType;
  stakeChips: number | null;
  hostUserId: string;
  hostDisplayName: string;
  players: Array<{ userId: string; displayName: string; ready: boolean; avatarUrl?: string | null }>;
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

export interface OAuthTokenResultPayload {
  ok: boolean;
  accessToken: string | null;
  error: string | null;
  detail: string | null;
}

export type ServerMessage =
  | { type: "ready" }
  | { type: "session_context"; payload: SessionContextPayload }
  | { type: "pong" }
  | { type: "room_state"; payload: RoomSnapshot }
  | { type: "room_list"; payload: RoomSnapshot[] }
  | { type: "game_state"; payload: GameSnapshot }
  | { type: "balance_state"; payload: BalanceSnapshot }
  | { type: "balance_debug"; payload: BalanceDebugSnapshot }
  | { type: "oauth_token_result"; payload: OAuthTokenResultPayload }
  | { type: "error"; message: string };
