export type ActivityMode = "server" | "casual";

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
}

export interface RoomSnapshot {
  instanceId: string;
  guildId: string | null;
  channelId: string | null;
  mode: ActivityMode;
  players: RoomPlayer[];
  createdAt: number;
}

export interface ActivityBootstrap {
  sdkReady: boolean;
  clientId: string | null;
  context: ActivityContext;
  currentUser: ActivityUser;
}
