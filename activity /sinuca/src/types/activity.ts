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
  avatarUrl?: string | null;
}

export interface SessionContextPayload {
  userId: string | null;
  displayName: string | null;
  guildId: string | null;
  channelId: string | null;
  instanceId: string | null;
}

export interface ActivityBootstrap {
  sdkReady: boolean;
  clientId: string | null;
  context: ActivityContext;
  currentUser: ActivityUser;
  bootDebug: string[];
}
