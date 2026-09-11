import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const MUSIC_CACHE_MS = 5_000;
const DEFAULT_WORKER_OFFLINE_SECONDS = 90;

let musicCache: { checkedAt: number; available: boolean } | null = null;

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function workerRegistryPath(): string {
  const configured = String(process.env.CORE_WORKERS_REGISTRY_PATH || "").trim();
  if (configured) return resolve(configured);
  return fileURLToPath(new URL("../../../../data/core_workers_registry.json", import.meta.url));
}

function normalizedCapabilities(record: Record<string, unknown>): Set<string> {
  const values = [
    ...stringList(record.roles),
    ...stringList(record.manual_roles),
    ...stringList(record.capabilities),
    ...stringList(record.manual_capabilities),
  ];
  return new Set(values.map((value) => value.toLowerCase().replace(/_/g, "-")));
}

function timestampSeconds(value: unknown): number {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

export function dashboardMusicWorkerRecordAvailable(
  candidate: unknown,
  nowSeconds: number,
  offlineAfterSeconds: number,
): boolean {
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return false;
  const worker = candidate as Record<string, unknown>;
  if (worker.enabled === false) return false;
  const runtimeKind = String(worker.runtime_kind || "").trim().toLowerCase();
  const source = String(worker.source || "").trim().toLowerCase();
  if (runtimeKind === "apk" || source.startsWith("core-worker-apk")) return false;
  const lastSeen = timestampSeconds(worker.last_heartbeat_at || worker.updated_at);
  if (!lastSeen || nowSeconds - lastSeen > offlineAfterSeconds) return false;
  const capabilities = normalizedCapabilities(worker);
  return capabilities.has("phone-worker") && capabilities.has("music");
}

export function dashboardMusicWorkerAvailable(now = Date.now()): boolean {
  if (musicCache && now - musicCache.checkedAt <= MUSIC_CACHE_MS) return musicCache.available;

  let available = false;
  try {
    const raw = JSON.parse(readFileSync(workerRegistryPath(), "utf8")) as { workers?: unknown };
    const records = raw.workers && typeof raw.workers === "object" && !Array.isArray(raw.workers)
      ? Object.values(raw.workers as Record<string, unknown>)
      : [];
    const offlineAfter = Math.max(
      15,
      Number.parseInt(String(process.env.CORE_WORKER_OFFLINE_AFTER_SECONDS || DEFAULT_WORKER_OFFLINE_SECONDS), 10)
        || DEFAULT_WORKER_OFFLINE_SECONDS,
    );
    const nowSeconds = now / 1000;
    available = records.some((candidate) => dashboardMusicWorkerRecordAvailable(candidate, nowSeconds, offlineAfter));
  } catch {
    available = false;
  }

  musicCache = { checkedAt: now, available };
  return available;
}

export function resetDashboardMusicWorkerCacheForTests(): void {
  musicCache = null;
}
