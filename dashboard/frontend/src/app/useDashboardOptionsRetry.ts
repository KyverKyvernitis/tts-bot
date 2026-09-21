import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { DashboardOptionsPayload } from "../types/dashboard";
import { fetchDashboardOptions } from "../transport/dashboardApi";
import type { DashboardNotice } from "./useDashboardSessionBootstrap";
import { errorText } from "./errors";

export function useDashboardOptionsRetry(guildId: string | null, activeGuildRef: MutableRefObject<string | null>, setOptions: Dispatch<SetStateAction<DashboardOptionsPayload | null>>, setNotice: Dispatch<SetStateAction<DashboardNotice | null>>) {
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => { request.current?.abort(); request.current = null; setBusy(false); return () => request.current?.abort(); }, [guildId]);
  const retry = useCallback(async () => {
    if (!guildId || request.current) return;
    const controller = new AbortController(); request.current = controller; setBusy(true);
    try {
      const options = await fetchDashboardOptions(guildId, controller.signal);
      if (controller.signal.aborted || activeGuildRef.current !== guildId) return;
      if (!options.ok) throw new Error("Não foi possível carregar os canais e cargos. Tente novamente.");
      setOptions(options);
    } catch (error) {
      if (!controller.signal.aborted && activeGuildRef.current === guildId) setNotice({ type: "error", text: errorText(error) });
    } finally { if (request.current === controller) { request.current = null; setBusy(false); } }
  }, [guildId, activeGuildRef, setOptions, setNotice]);
  return { optionsBusy: busy, retryOptions: retry };
}
