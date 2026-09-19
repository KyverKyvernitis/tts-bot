import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export interface DashboardWorkerAvailabilitySpec {
  required_capabilities?: unknown;
  excluded_runtime_kinds?: unknown;
  excluded_source_prefixes?: unknown;
  offline_after_seconds?: unknown;
  offline_after_env?: unknown;
  cache_ms?: unknown;
}

interface NormalizedWorkerAvailabilitySpec {
  requiredCapabilities: string[];
  excludedRuntimeKinds: Set<string>;
  excludedSourcePrefixes: string[];
  offlineAfterSeconds: number;
  cacheMs: number;
}

const availabilityCache = new Map<string, { checkedAt: number; available: boolean }>();
const specCache = new Map<string, DashboardWorkerAvailabilitySpec>();

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function normalizeCapability(value: string): string {
  return value.toLowerCase().replace(/_/g, "-");
}

function workerRegistryPath(): string {
  const configured = String(process.env.CORE_WORKERS_REGISTRY_PATH || "").trim();
  if (configured) return resolve(configured);
  return fileURLToPath(new URL("../../../../data/core_workers_registry.json", import.meta.url));
}

function repositoryPath(relativePath: string): string {
  const root = fileURLToPath(new URL("../../../../", import.meta.url));
  return resolve(root, relativePath);
}

function readSpec(relativePath: string): DashboardWorkerAvailabilitySpec {
  const cached = specCache.get(relativePath);
  if (cached) return cached;
  const parsed = JSON.parse(readFileSync(repositoryPath(relativePath), "utf8")) as DashboardWorkerAvailabilitySpec;
  specCache.set(relativePath, parsed);
  return parsed;
}

function normalizeSpec(spec: DashboardWorkerAvailabilitySpec): NormalizedWorkerAvailabilitySpec {
  const envKey = String(spec.offline_after_env || "").trim();
  const configuredOffline = envKey ? Number.parseInt(String(process.env[envKey] || ""), 10) : Number.NaN;
  const defaultOffline = Number.parseInt(String(spec.offline_after_seconds || 90), 10) || 90;
  const cacheMs = Math.max(0, Number.parseInt(String(spec.cache_ms || 5000), 10) || 5000);
  return {
    requiredCapabilities: stringList(spec.required_capabilities).map(normalizeCapability),
    excludedRuntimeKinds: new Set(stringList(spec.excluded_runtime_kinds).map((value) => value.toLowerCase())),
    excludedSourcePrefixes: stringList(spec.excluded_source_prefixes).map((value) => value.toLowerCase()),
    offlineAfterSeconds: Math.max(15, Number.isFinite(configuredOffline) ? configuredOffline : defaultOffline),
    cacheMs,
  };
}

function timestampSeconds(value: unknown): number {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric;
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

export function dashboardWorkerRecordAvailable(
  candidate: unknown,
  nowSeconds: number,
  specInput: DashboardWorkerAvailabilitySpec,
): boolean {
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return false;
  const worker = candidate as Record<string, unknown>;
  if (worker.enabled === false) return false;

  const spec = normalizeSpec(specInput);
  const runtimeKind = String(worker.runtime_kind || "").trim().toLowerCase();
  const source = String(worker.source || "").trim().toLowerCase();
  if (spec.excludedRuntimeKinds.has(runtimeKind)) return false;
  if (spec.excludedSourcePrefixes.some((prefix) => source.startsWith(prefix))) return false;

  const lastSeen = timestampSeconds(worker.last_heartbeat_at || worker.updated_at);
  if (!lastSeen || nowSeconds - lastSeen > spec.offlineAfterSeconds) return false;

  const capabilities = new Set([
    ...stringList(worker.roles),
    ...stringList(worker.manual_roles),
    ...stringList(worker.capabilities),
    ...stringList(worker.manual_capabilities),
  ].map(normalizeCapability));
  return spec.requiredCapabilities.every((capability) => capabilities.has(capability));
}

export function dashboardWorkerAvailable(specPath: string, now = Date.now()): boolean {
  const specInput = readSpec(specPath);
  const spec = normalizeSpec(specInput);
  const cached = availabilityCache.get(specPath);
  if (cached && now - cached.checkedAt <= spec.cacheMs) return cached.available;

  let available = false;
  try {
    const raw = JSON.parse(readFileSync(workerRegistryPath(), "utf8")) as { workers?: unknown };
    const records = raw.workers && typeof raw.workers === "object" && !Array.isArray(raw.workers)
      ? Object.values(raw.workers as Record<string, unknown>)
      : [];
    const nowSeconds = now / 1000;
    available = records.some((candidate) => dashboardWorkerRecordAvailable(candidate, nowSeconds, specInput));
  } catch {
    available = false;
  }

  availabilityCache.set(specPath, { checkedAt: now, available });
  return available;
}

export function resetDashboardWorkerAvailabilityCacheForTests(): void {
  availabilityCache.clear();
  specCache.clear();
}
