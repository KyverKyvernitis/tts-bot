export type Route =
  | { page: "landing" }
  | { page: "privacy" }
  | { page: "terms" }
  | { page: "servers" }
  | { page: "invite"; guildId: string }
  | { page: "dashboard"; guildId: string; view: "general" | "modules" | "commands" | "module"; moduleId: string | null };

export type DashboardRoute = Extract<Route, { page: "dashboard" }>;

export function isSnowflake(value: string | undefined | null): value is string {
  return Boolean(value && /^\d{15,25}$/.test(value));
}

export function parseRoute(pathname: string): Route {
  if (pathname === "/privacy" || pathname === "/privacidade") return { page: "privacy" };
  if (pathname === "/terms" || pathname === "/termos") return { page: "terms" };
  if (pathname === "/dashboard" || pathname === "/dashboard/") return { page: "servers" };

  const invite = pathname.match(/^\/dashboard\/invite\/(\d{15,25})\/?$/);
  if (invite) return { page: "invite", guildId: invite[1] };

  const dashboard = pathname.match(/^\/dashboard\/(\d{15,25})(?:\/(.*?))?\/?$/i);
  if (!dashboard) return { page: "landing" };

  const guildId = dashboard[1];
  const segments = String(dashboard[2] || "").split("/").filter(Boolean);
  if (segments.length === 0 || (segments.length === 1 && ["geral", "general"].includes(segments[0].toLowerCase()))) {
    return { page: "dashboard", guildId, view: "general", moduleId: null };
  }
  if (segments.length === 1 && segments[0].toLowerCase() === "modulos") {
    return { page: "dashboard", guildId, view: "modules", moduleId: null };
  }
  if (segments.length === 1 && segments[0].toLowerCase() === "comandos") {
    return { page: "dashboard", guildId, view: "commands", moduleId: null };
  }
  if (segments.length === 2 && segments[0].toLowerCase() === "modulos" && /^[a-z0-9_-]+$/i.test(segments[1])) {
    return { page: "dashboard", guildId, view: "module", moduleId: segments[1] };
  }
  if (segments.length === 1 && /^[a-z0-9_-]+$/i.test(segments[0])) {
    return { page: "dashboard", guildId, view: "module", moduleId: segments[0] };
  }
  return { page: "dashboard", guildId, view: "modules", moduleId: null };
}

export function routePath(route: Route): string {
  if (route.page === "privacy") return "/privacy";
  if (route.page === "terms") return "/terms";
  if (route.page === "servers") return "/dashboard";
  if (route.page === "invite") return `/dashboard/invite/${route.guildId}`;
  if (route.page === "dashboard") {
    if (route.view === "modules") return `/dashboard/${route.guildId}/modulos`;
    if (route.view === "commands") return `/dashboard/${route.guildId}/comandos`;
    if (route.view === "module" && route.moduleId) return `/dashboard/${route.guildId}/modulos/${route.moduleId}`;
    return `/dashboard/${route.guildId}/geral`;
  }
  return "/";
}
