export function isLongLikeObject(value: unknown): boolean {
  return typeof value === "object"
    && value !== null
    && typeof (value as { low?: unknown }).low === "number"
    && typeof (value as { high?: unknown }).high === "number";
}

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value));
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value) || isLongLikeObject(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

export function deepMerge(defaults: Record<string, unknown>, raw: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = { ...defaults };
  for (const [key, value] of Object.entries(raw)) {
    if (isPlainObject(value) && isPlainObject(defaults[key])) result[key] = deepMerge(defaults[key] as Record<string, unknown>, value);
    else result[key] = value;
  }
  return result;
}

export function getPath(source: Record<string, unknown>, path: string): unknown {
  let current: unknown = source;
  for (const part of path.split(".")) {
    if (!part) continue;
    if (typeof current !== "object" || current === null || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

export function setPath(target: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split(".").filter(Boolean);
  if (!parts.length) return;
  let current = target;
  for (const part of parts.slice(0, -1)) {
    if (!isPlainObject(current[part])) current[part] = {};
    current = current[part] as Record<string, unknown>;
  }
  current[parts[parts.length - 1]] = value;
}

export function dotSetForPath(target: Record<string, unknown>, path: string, value: unknown): void {
  const cleanPath = path.split(".").filter(Boolean).join(".");
  if (cleanPath && !cleanPath.includes("$") && !cleanPath.includes("..")) target[cleanPath] = value;
}
