import type { Request, Response } from "express";

const rateBuckets = new Map<string, { count: number; resetAt: number }>();
const MAX_RATE_BUCKETS = 10_000;
let rateLimitChecks = 0;

export function sendNoStoreJson(res: Response, status: number, payload: unknown) {
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.type("application/json");
  res.status(status).json(payload);
}

export function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "bigint") return String(value).trim();
  }
  return "";
}

export function dashboardGuildId(req: Request): string {
  const body = req.body && typeof req.body === "object" ? req.body as Record<string, unknown> : {};
  return firstString(req.params.guildId, req.query.guild_id, body.guild_id, body.guildId);
}

export function isSecureRequest(req: Request): boolean {
  return req.secure || firstString(req.headers["x-forwarded-proto"]).split(",")[0].trim().toLowerCase() === "https";
}

export function requestOrigin(req: Request, configuredOrigin: string): string {
  if (configuredOrigin) return configuredOrigin;
  if (process.env.NODE_ENV === "production") return "";
  const protocol = isSecureRequest(req) ? "https" : "http";
  const host = firstString(req.headers["x-forwarded-host"], req.headers.host);
  return host ? `${protocol}://${host}` : "";
}

export function callbackUrl(req: Request, configuredOrigin: string): string {
  const origin = requestOrigin(req, configuredOrigin);
  return origin ? `${origin}/api/auth/callback` : "";
}

export function mutationOriginAllowed(req: Request, configuredOrigin: string, allowedOrigins: Set<string>): boolean {
  const origin = firstString(req.headers.origin);
  if (!origin) return true;
  try {
    const normalized = new URL(origin).origin;
    const expected = requestOrigin(req, configuredOrigin);
    return normalized === expected || allowedOrigins.has(normalized);
  } catch {
    return false;
  }
}

export function takeRateLimit(req: Request, bucket: string, limit: number, windowMs: number): boolean {
  const key = `${bucket}:${req.ip || req.socket.remoteAddress || "unknown"}`;
  const now = Date.now();
  rateLimitChecks += 1;
  if (rateLimitChecks % 128 === 0 || rateBuckets.size >= MAX_RATE_BUCKETS) {
    for (const [storedKey, entry] of rateBuckets) {
      if (entry.resetAt <= now) rateBuckets.delete(storedKey);
    }
  }
  if (!rateBuckets.has(key) && rateBuckets.size >= MAX_RATE_BUCKETS) return false;
  const current = rateBuckets.get(key);
  if (!current || current.resetAt <= now) {
    rateBuckets.set(key, { count: 1, resetAt: now + windowMs });
    return true;
  }
  if (current.count >= limit) return false;
  current.count += 1;
  return true;
}

export function appendCookie(res: Response, value: string) {
  res.append("Set-Cookie", value);
}

export function authErrorRedirect(req: Request, publicOrigin: string, code: string): string {
  const origin = requestOrigin(req, publicOrigin);
  const url = new URL(origin || "http://localhost");
  url.pathname = "/";
  url.searchParams.set("auth_error", code);
  return `${url.pathname}${url.search}`;
}
