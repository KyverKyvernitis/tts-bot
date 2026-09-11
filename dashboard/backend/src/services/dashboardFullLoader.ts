export type DashboardLoadStep = "settings" | "summary" | "options" | "bot";

export interface DashboardFullIdentity {
  id: string;
  username?: string | null;
  global_name?: string | null;
  avatar?: string | null;
  avatarUrl?: string | null;
}

export interface DashboardFullAuth {
  guildId: string;
  user: DashboardFullIdentity;
}

export interface DashboardFullLoaderDependencies {
  configService: {
    getSettings(guildId: string): Promise<{ sections: unknown[]; values: Record<string, unknown> }>;
    getSummary(guildId: string): Promise<{ sections: unknown[] }>;
  };
  listGuildOptions(guildId: string): Promise<{
    ok: boolean;
    channels: unknown[];
    roles: unknown[];
    error?: string | null;
  }>;
  getBotIdentity(): Promise<DashboardFullIdentity | null>;
}

export function createDashboardFullLoader(dependencies: DashboardFullLoaderDependencies) {
  return async function loadFullDashboard(
    auth: DashboardFullAuth,
    onTaskCompleted?: (step: DashboardLoadStep) => void,
  ) {
    const track = async <T>(step: DashboardLoadStep, task: Promise<T>): Promise<T> => {
      const result = await task;
      onTaskCompleted?.(step);
      return result;
    };
    const [settings, summary, optionsResult, bot] = await Promise.all([
      track("settings", dependencies.configService.getSettings(auth.guildId)),
      track("summary", dependencies.configService.getSummary(auth.guildId)),
      track("options", dependencies.listGuildOptions(auth.guildId).catch(() => ({ ok: false, channels: [], roles: [], error: "options_failed" }))),
      track("bot", dependencies.getBotIdentity().catch(() => null)),
    ]);
    return {
      ok: true as const,
      guildId: auth.guildId,
      user: auth.user,
      bot,
      sections: settings.sections,
      values: settings.values,
      summary: summary.sections,
      options: {
        ok: optionsResult.ok,
        guildId: auth.guildId,
        channels: optionsResult.channels,
        roles: optionsResult.roles,
        error: optionsResult.error ?? null,
      },
    };
  };
}
