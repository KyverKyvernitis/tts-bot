import type { DashboardFieldDefinition, DashboardSectionDefinition } from "../types/dashboard";
import { valuesEqual } from "./dashboardValues";
import { routePath, type Route } from "./routing";

export function selectedSectionIdForRoute(route: Route): string | null {
  if (route.page !== "dashboard") return null;
  if (route.view === "general") return "general";
  if (route.view === "module") return route.moduleId;
  return null;
}

export function changedFieldsForSection(
  section: DashboardSectionDefinition | null,
  values: Record<string, unknown>,
  draft: Record<string, unknown>,
): DashboardFieldDefinition[] {
  return section?.fields.filter((field) => !valuesEqual(values[field.id], draft[field.id])) ?? [];
}

export function isProtectedRoute(route: Route): boolean {
  return route.page === "servers" || route.page === "invite" || route.page === "dashboard";
}

export function loginReturnPath(route: Route): string {
  return route.page === "landing" || route.page === "privacy" || route.page === "terms"
    ? "/dashboard"
    : routePath(route);
}

export function saveSuccessText(count: number, summaryError: boolean): string {
  const countText = count === 1 ? "1 alteração salva." : `${count} alterações salvas.`;
  return summaryError
    ? `${countText} O resumo será atualizado na próxima abertura.`
    : `${countText} O bot sincronizará os módulos compatíveis automaticamente.`;
}
