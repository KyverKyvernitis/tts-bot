import { createContext } from "react";

export interface ModulePreviewIdentity { botName?: string; botAvatarUrl?: string | null; guildName?: string; guildAvatarUrl?: string | null }
export const ModulePreviewContext = createContext<ModulePreviewIdentity>({});
