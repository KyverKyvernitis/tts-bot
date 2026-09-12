import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { parseRoute, routePath, type Route } from "./routing";
import type { DashboardNotice } from "./useDashboardSessionBootstrap";

interface DashboardLoadState {
  generation: number;
  controller: AbortController | null;
}

interface UseDashboardBrowserNavigationOptions {
  route: Route;
  selectedSectionId: string | null;
  values: Record<string, unknown>;
  hasUnsavedChanges: boolean;
  messageEditorActive: boolean;
  setRoute: Dispatch<SetStateAction<Route>>;
  setDraft: Dispatch<SetStateAction<Record<string, unknown>>>;
  setNotice: Dispatch<SetStateAction<DashboardNotice | null>>;
  setMobileMenuOpen: Dispatch<SetStateAction<boolean>>;
  setMessageEditorActive: Dispatch<SetStateAction<boolean>>;
  dashboardLoadRef: MutableRefObject<DashboardLoadState>;
  activeGuildRef: MutableRefObject<string | null>;
}

export function useDashboardBrowserNavigation(options: UseDashboardBrowserNavigationOptions): void {
  const {
    route,
    selectedSectionId,
    values,
    hasUnsavedChanges,
    messageEditorActive,
    setRoute,
    setDraft,
    setNotice,
    setMobileMenuOpen,
    setMessageEditorActive,
    dashboardLoadRef,
    activeGuildRef,
  } = options;
  const currentRoutePath = routePath(route);

  useEffect(() => {
    activeGuildRef.current = route.page === "dashboard" ? route.guildId : null;
  }, [activeGuildRef, route]);

  useEffect(() => () => dashboardLoadRef.current.controller?.abort(), [dashboardLoadRef]);

  useEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [currentRoutePath]);

  useEffect(() => {
    const normalizedPath = routePath(parseRoute(window.location.pathname));
    if (normalizedPath === window.location.pathname) return;
    window.history.replaceState({}, "", `${normalizedPath}${window.location.search}${window.location.hash}`);
  }, []);

  useEffect(() => {
    const onPopState = () => {
      if (messageEditorActive) {
        window.dispatchEvent(new Event("osk:message-editor-back"));
        return;
      }
      if (hasUnsavedChanges) {
        if (!window.confirm("Descartar as alterações que ainda não foram salvas?")) {
          window.history.pushState({}, "", routePath(route));
          return;
        }
        setDraft(values);
      }
      setRoute(parseRoute(window.location.pathname));
      setNotice(null);
      setMobileMenuOpen(false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [hasUnsavedChanges, messageEditorActive, route, setDraft, setMobileMenuOpen, setNotice, setRoute, values]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!hasUnsavedChanges) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    setMessageEditorActive(false);
  }, [selectedSectionId, setMessageEditorActive]);
}
