export type ActivityMode = "server" | "casual";
export type TableType = "stake" | "casual";
export type RoomStatus = "waiting" | "ready" | "in_game";
export type GameStatus = "waiting_shot" | "finished";

export interface ActivityContext {
  mode: ActivityMode;
  instanceId: string | null;
  guildId: string | null;
  channelId: string | null;
  source: "query" | "fallback";
}

export interface ActivityUser {
  userId: string;
  displayName: string;
  avatarUrl?: string | null;
}

export interface SessionContextPayload {
  userId: string | null;
  displayName: string | null;
  guildId: string | null;
  channelId: string | null;
  instanceId: string | null;
}

export interface RoomPlayer {
  userId: string;
  displayName: string;
  ready: boolean;
  avatarUrl?: string | null;
}

export interface RoomSnapshot {
  roomId: string;
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: ActivityMode;
  tableType: TableType;
  stakeChips: number | null;
  hostUserId: string;
  hostDisplayName: string;
  players: RoomPlayer[];
  status: RoomStatus;
  stakeLabel: string;
  createdAt: number;
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
  turnUserId: string;
  shotSequence: number;
  balls: GameBallSnapshot[];
  createdAt: number;
  updatedAt: number;
  lastShot: GameShotSnapshot | null;
}

export interface BalanceSnapshot {
  chips: number;
  bonusChips: number;
}

export interface ActivityBootstrap {
  sdkReady: boolean;
  clientId: string | null;
  context: ActivityContext;
  currentUser: ActivityUser;
  bootDebug: string[];
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
