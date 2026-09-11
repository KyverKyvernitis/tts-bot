import { createHash } from "crypto";

export interface ShortCacheEntry<T> {
  expiresAt: number;
  value: T;
}

export function tokenCacheKey(token: string): string {
  return createHash("sha256").update(token).digest("base64url");
}

export function getShortCacheValue<T>(
  cache: Map<string, ShortCacheEntry<T>>,
  key: string,
  now = Date.now(),
): T | null {
  const entry = cache.get(key);
  if (!entry) return null;
  if (entry.expiresAt <= now) {
    cache.delete(key);
    return null;
  }
  return entry.value;
}

export function setShortCacheValue<T>(
  cache: Map<string, ShortCacheEntry<T>>,
  key: string,
  value: T,
  ttlMs: number,
  options: { now?: number; maxEntries?: number } = {},
): void {
  const now = options.now ?? Date.now();
  const maxEntries = Math.max(1, options.maxEntries ?? 2_000);

  if (cache.size >= maxEntries) {
    for (const [entryKey, entry] of cache) {
      if (entry.expiresAt <= now) cache.delete(entryKey);
    }
  }
  while (cache.size >= maxEntries) {
    const oldest = cache.keys().next().value as string | undefined;
    if (!oldest) break;
    cache.delete(oldest);
  }

  cache.set(key, { expiresAt: now + Math.max(0, ttlMs), value });
}
