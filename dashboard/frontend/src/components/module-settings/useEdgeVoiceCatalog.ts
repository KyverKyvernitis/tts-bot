import { useEffect, useState } from "react";
import { fetchEdgeVoiceCatalog, type EdgeVoiceCatalog } from "../../transport/ttsVoiceCatalog";

let cached: { catalog: EdgeVoiceCatalog; expiresAt: number } | null = null;
export function useEdgeVoiceCatalog() {
  const [catalog, setCatalog] = useState<EdgeVoiceCatalog>(() => cached?.catalog || { voices: [], status: "unavailable" });
  const [loading, setLoading] = useState(!cached || cached.expiresAt <= Date.now());
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!revision && cached && cached.expiresAt > Date.now()) { setCatalog(cached.catalog); setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    fetchEdgeVoiceCatalog(controller.signal, revision > 0).then(next => {
      if (controller.signal.aborted) return;
      cached = { catalog: next, expiresAt: Date.now() + (next.status === "ready" ? 60_000 : 5_000) };
      setCatalog(next);
    }).catch(() => {
      if (!controller.signal.aborted) setCatalog(current => ({ voices: current.voices, status: current.voices.length ? "cached" : "unavailable" }));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  return { ...catalog, loading, retry: () => setRevision(value => value + 1) };
}
