import { createContext } from "react";

export interface MessageMediaIdentity {
  guildName?: string;
  guildAvatarUrl?: string | null;
  botAvatarUrl?: string | null;
}

export const MessageMediaContext = createContext<MessageMediaIdentity>({});
