export type ActivityMode = "server" | "casual";

export interface ActivityContext {
  mode: ActivityMode;
  instanceId: string | null;
  guildId: string | null;
  channelId: string | null;
  source: "query" | "fallback";
}

export interface ActivityBootstrap {
  sdkReady: boolean;
  clientId: string | null;
  context: ActivityContext;
}
