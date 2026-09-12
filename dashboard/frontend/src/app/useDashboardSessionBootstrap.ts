import { useEffect, useState } from "react";
import { syncSiteIcon } from "../siteIcon";
import { fetchDashboardIdentity } from "../transport/dashboardApi";
import { fetchDashboardSession } from "../transport/sessionApi";
import type { DashboardSupportServerPayload, DashboardUserPayload } from "../types/dashboard";
import { LOAD_COMPLETE_HOLD_MS } from "./constants";
import { sessionResultFromError, type BootSessionResult } from "./sessionModel";

export type DashboardSessionState = "loading" | "authenticated" | "anonymous";
export type DashboardNotice = { type: "error" | "success" | "info"; text: string };

export function useDashboardSessionBootstrap(onNotice: (notice: DashboardNotice | null) => void) {
  const [sessionState, setSessionState] = useState<DashboardSessionState>("loading");
  const [user, setUser] = useState<DashboardUserPayload | null>(null);
  const [botIdentity, setBotIdentity] = useState<DashboardUserPayload | null>(null);
  const [supportServer, setSupportServer] = useState<DashboardSupportServerPayload | null>(null);
  const [bootProgress, setBootProgress] = useState(0);

  useEffect(() => {
    syncSiteIcon(botIdentity?.avatarUrl);
  }, [botIdentity?.avatarUrl]);

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let sessionFinished = false;
    let finishTimer: number | null = null;
    const authError = new URLSearchParams(window.location.search).get("auth_error");
    if (authError) {
      onNotice({ type: "error", text: `Não foi possível concluir o login (${authError}).` });
      window.history.replaceState({}, "", window.location.pathname);
    }
    setBootProgress(0);
    void fetchDashboardIdentity(controller.signal)
      .then((payload) => {
        if (disposed) return;
        setBotIdentity(payload.bot || null);
        setSupportServer(payload.supportServer || null);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!disposed && !sessionFinished) setBootProgress(50);
      });
    void fetchDashboardSession(controller.signal)
      .then((session): BootSessionResult => ({
        state: session.authenticated ? "authenticated" : "anonymous",
        user: session.user || null,
      }))
      .catch(sessionResultFromError)
      .then((session) => {
        if (disposed) return;
        sessionFinished = true;
        setBootProgress(100);
        finishTimer = window.setTimeout(() => {
          if (disposed) return;
          setUser(session.user);
          if (session.notice) onNotice(session.notice);
          setSessionState(session.state);
        }, LOAD_COMPLETE_HOLD_MS);
      });

    return () => {
      disposed = true;
      controller.abort();
      if (finishTimer !== null) window.clearTimeout(finishTimer);
    };
  }, []); // onNotice is the stable React state setter supplied by App.

  return {
    sessionState,
    setSessionState,
    user,
    setUser,
    botIdentity,
    setBotIdentity,
    supportServer,
    bootProgress,
  };
}
