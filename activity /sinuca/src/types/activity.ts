export type ActivityMode = "server" | "casual";
export type RoomStatus = "waiting" | "ready" | "in_game";

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
}

export interface RoomSnapshot {
  roomId: string;
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: ActivityMode;
  hostUserId: string;
  hostDisplayName: string;
  players: RoomPlayer[];
  status: RoomStatus;
  stakeLabel: string;
  createdAt: number;
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
