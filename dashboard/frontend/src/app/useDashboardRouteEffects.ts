import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { DashboardSectionDefinition, DashboardServerCard } from "../types/dashboard";
import { routePath, type DashboardRoute, type Route } from "./routing";
import type { DashboardNotice, DashboardSessionState } from "./useDashboardSessionBootstrap";

interface DashboardRouteEffectsOptions {
  route: Route;
  sessionState: DashboardSessionState;
  manageable: DashboardServerCard[];
  serversLoaded: boolean;
  sections: DashboardSectionDefinition[];
  selectedSection: DashboardSectionDefinition | null;
  loadingDashboard: boolean;
  loadServers(force?: boolean): Promise<void>;
  loadDashboard(guildId: string, quiet?: boolean): Promise<void>;
  setSelectedServer: Dispatch<SetStateAction<DashboardServerCard | null>>;
  setRoute: Dispatch<SetStateAction<Route>>;
  setNotice: Dispatch<SetStateAction<DashboardNotice | null>>;
}

export function useDashboardRouteEffects(options: DashboardRouteEffectsOptions): void {
  const {
    route,
    sessionState,
    manageable,
    serversLoaded,
    sections,
    selectedSection,
    loadingDashboard,
    loadServers,
    loadDashboard,
    setSelectedServer,
    setRoute,
    setNotice,
  } = options;

  useEffect(() => {
    if (sessionState !== "authenticated") return;
    if (["servers", "invite", "dashboard"].includes(route.page)) void loadServers();
  }, [loadServers, route.page, sessionState]);

  const activeDashboardGuildId = route.page === "dashboard" ? route.guildId : null;
  useEffect(() => {
    if (!activeDashboardGuildId || sessionState !== "authenticated") return;
    void loadDashboard(activeDashboardGuildId);
  }, [activeDashboardGuildId, loadDashboard, sessionState]);

  useEffect(() => {
    if (route.page !== "dashboard" || route.view !== "module" || !route.moduleId || sections.length === 0 || selectedSection || loadingDashboard) return;
    const next: DashboardRoute = { page: "dashboard", guildId: route.guildId, view: "modules", moduleId: null };
    window.history.replaceState({}, "", routePath(next));
    setRoute(next);
    setNotice({ type: "info", text: "Esse módulo não existe mais. Voltamos para Módulos." });
  }, [loadingDashboard, route, sections.length, selectedSection, setNotice, setRoute]);

  useEffect(() => {
    if (route.page !== "dashboard" || !serversLoaded) return;
    const server = manageable.find((item) => item.id === route.guildId) || null;
    if (server) setSelectedServer(server);
  }, [manageable, route, serversLoaded, setSelectedServer]);
}
