import { registerDashboardApiMutationRoutes } from "./dashboardApiMutationRoutes.js";
import { registerDashboardApiReadRoutes } from "./dashboardApiReadRoutes.js";
import type { RegisterDashboardApiRoutesOptions } from "./dashboardApiRouteTypes.js";

export type { RegisterDashboardApiRoutesOptions } from "./dashboardApiRouteTypes.js";

export function registerDashboardApiRoutes(options: RegisterDashboardApiRoutesOptions) {
  registerDashboardApiReadRoutes(options);
  registerDashboardApiMutationRoutes(options);
}
