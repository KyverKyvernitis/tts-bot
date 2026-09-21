import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { fetchDashboardFull, fetchDashboardServers } from "../transport/dashboardApi";
import { DashboardHttpError } from "../transport/httpClient";
import type {
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardSectionSummary,
  DashboardServerCard,
  DashboardUserPayload,
} from "../types/dashboard";
import { LOAD_COMPLETE_HOLD_MS } from "./constants";
import { errorText } from "./errors";
import { isSnowflake } from "./routing";
import type { DashboardNotice, DashboardSessionState } from "./useDashboardSessionBootstrap";

interface UseDashboardDataOptions {
  sessionState: DashboardSessionState;
  setSessionState: Dispatch<SetStateAction<DashboardSessionState>>;
  setUser: Dispatch<SetStateAction<DashboardUserPayload | null>>;
  setBotIdentity: Dispatch<SetStateAction<DashboardUserPayload | null>>;
  setNotice: Dispatch<SetStateAction<DashboardNotice | null>>;
}

export function useDashboardData({ sessionState, setSessionState, setUser, setBotIdentity, setNotice }: UseDashboardDataOptions) {
  const [manageable, setManageable] = useState<DashboardServerCard[]>([]);
  const [needsInvite, setNeedsInvite] = useState<DashboardServerCard[]>([]);
  const [serversLoaded, setServersLoaded] = useState(false);
  const [loadingServers, setLoadingServers] = useState(false);
  const [selectedServer, setSelectedServer] = useState<DashboardServerCard | null>(null);
  const [sections, setSections] = useState<DashboardSectionDefinition[]>([]);
  const [summary, setSummary] = useState<DashboardSectionSummary[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [guildOptions, setGuildOptions] = useState<DashboardOptionsPayload | null>(null);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [dashboardProgress, setDashboardProgress] = useState(0);
  const dashboardLoadRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const loadedGuildRef = useRef<string | null>(null);
  const activeGuildRef = useRef<string | null>(null);

  const loadServers = useCallback(async (force = false) => {
    if (sessionState !== "authenticated" || (serversLoaded && !force)) return;
    setLoadingServers(true);
    try {
      const payload = await fetchDashboardServers();
      setManageable(payload.manageable || []);
      setNeedsInvite(payload.needsInvite || []);
      if (payload.user) setUser(payload.user);
      setServersLoaded(true);
    } catch (error) {
      if (error instanceof DashboardHttpError && error.status === 401) {
        setSessionState("anonymous");
        setUser(null);
        loadedGuildRef.current = null;
      }
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      setLoadingServers(false);
    }
  }, [serversLoaded, sessionState, setNotice, setSessionState, setUser]);

  const loadDashboard = useCallback(async (guildId: string, quiet = false) => {
    if (!isSnowflake(guildId) || sessionState !== "authenticated") return;
    dashboardLoadRef.current.controller?.abort();
    const controller = new AbortController();
    const generation = dashboardLoadRef.current.generation + 1;
    dashboardLoadRef.current = { generation, controller };
    const changingGuild = loadedGuildRef.current !== guildId;
    if (!quiet) {
      setDashboardProgress(0);
      setLoadingDashboard(true);
      if (changingGuild) {
        setSections([]);
        setSummary([]);
        setValues({});
        setDraft({});
        setGuildOptions(null);
      }
    }
    let completedSuccessfully = false;
    try {
      const payload = await fetchDashboardFull(guildId, controller.signal, (progress) => {
        if (quiet || dashboardLoadRef.current.generation !== generation || activeGuildRef.current !== guildId) return;
        setDashboardProgress((current) => Math.max(current, progress));
      });
      if (dashboardLoadRef.current.generation !== generation || activeGuildRef.current !== guildId) return;
      loadedGuildRef.current = guildId;
      if (payload.user) setUser(payload.user);
      if (payload.bot) setBotIdentity(payload.bot);
      setSections(payload.sections || []);
      setValues(payload.values || {});
      setDraft(payload.values || {});
      setSummary(payload.summary || []);
      setGuildOptions(payload.options || { ok: false, channels: [], roles: [], error: "options_unavailable" });
      setNotice(quiet ? { type: "success", text: "Dados atualizados com os valores persistidos." } : null);
      completedSuccessfully = true;
      if (!quiet) setDashboardProgress(100);
    } catch (error) {
      if (error instanceof DashboardHttpError && error.code === "aborted") return;
      if (dashboardLoadRef.current.generation !== generation) return;
      if (error instanceof DashboardHttpError && error.status === 401) {
        setSessionState("anonymous");
        setUser(null);
        loadedGuildRef.current = null;
      }
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      if (dashboardLoadRef.current.generation === generation) {
        if (!quiet && completedSuccessfully) {
          await new Promise((resolve) => window.setTimeout(resolve, LOAD_COMPLETE_HOLD_MS));
        }
        if (dashboardLoadRef.current.generation === generation) {
          dashboardLoadRef.current.controller = null;
          setLoadingDashboard(false);
        }
      }
    }
  }, [sessionState, setBotIdentity, setNotice, setSessionState, setUser]);

  const resetServers = useCallback(() => {
    setManageable([]);
    setNeedsInvite([]);
    setServersLoaded(false);
    loadedGuildRef.current = null;
  }, []);

  return {
    manageable,
    needsInvite,
    serversLoaded,
    loadingServers,
    selectedServer,
    setSelectedServer,
    sections,
    summary,
    setSummary,
    values,
    setValues,
    draft,
    setDraft,
    guildOptions,
    setGuildOptions,
    loadingDashboard,
    dashboardProgress,
    dashboardLoadRef,
    loadedGuildRef,
    activeGuildRef,
    loadServers,
    loadDashboard,
    resetServers,
  };
}
