import { loadDashboardCommandsCatalog, resetDashboardCommandsCatalogCacheForTests } from "./dashboardCommandsCatalog.js";
import { buildDashboardCommandsPayload } from "./dashboardCommandsModel.js";
import { dashboardMusicWorkerAvailable, resetDashboardMusicWorkerCacheForTests } from "./dashboardMusicWorker.js";
import type { DashboardCommandContext, DashboardCommandsPayload } from "./dashboardCommandsTypes.js";

export type {
  DashboardCommandCategory,
  DashboardCommandContext,
  DashboardCommandEntry,
  DashboardCommandsPayload,
} from "./dashboardCommandsTypes.js";

export function buildDashboardCommands(context: DashboardCommandContext): DashboardCommandsPayload {
  return buildDashboardCommandsPayload(loadDashboardCommandsCatalog(), context, dashboardMusicWorkerAvailable());
}

export function resetDashboardCommandsCachesForTests(): void {
  resetDashboardCommandsCatalogCacheForTests();
  resetDashboardMusicWorkerCacheForTests();
}
