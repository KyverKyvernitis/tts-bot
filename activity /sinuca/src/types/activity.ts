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
