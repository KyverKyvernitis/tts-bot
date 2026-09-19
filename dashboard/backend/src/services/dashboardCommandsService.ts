import { loadDashboardCommandsCatalog, resetDashboardCommandsCatalogCacheForTests } from "./dashboardCommandsCatalog.js";
import { buildDashboardCommandsPayload } from "./dashboardCommandsModel.js";
import { dashboardWorkerAvailable, resetDashboardWorkerAvailabilityCacheForTests } from "./dashboardWorkerAvailability.js";
import type { DashboardCommandContext, DashboardCommandsPayload } from "./dashboardCommandsTypes.js";

export type {
  DashboardCommandCategory,
  DashboardCommandContext,
  DashboardCommandEntry,
  DashboardCommandsPayload,
} from "./dashboardCommandsTypes.js";

const MUSIC_WORKER_SPEC = "cogs/musica/site/dashboard-worker.json";

export function buildDashboardCommands(context: DashboardCommandContext): DashboardCommandsPayload {
  return buildDashboardCommandsPayload(loadDashboardCommandsCatalog(), context, dashboardWorkerAvailable(MUSIC_WORKER_SPEC));
}

export function resetDashboardCommandsCachesForTests(): void {
  resetDashboardCommandsCatalogCacheForTests();
  resetDashboardWorkerAvailabilityCacheForTests();
}
