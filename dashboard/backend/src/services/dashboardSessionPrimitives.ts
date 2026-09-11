import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from "crypto";

export const DASHBOARD_SESSION_COOKIE = "osk_dashboard_session";
export const DASHBOARD_OAUTH_COOKIE = "osk_dashboard_oauth";
export const DASHBOARD_OAUTH_STATE_LIFETIME_MS = 10 * 60 * 1000;

function base64Url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

export function safeDashboardReturnPath(value: unknown): string {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return "/dashboard";
  try {
    const parsed = new URL(raw, "https://dashboard.invalid");
    if (parsed.origin !== "https://dashboard.invalid") return "/dashboard";
    const dashboardPath = parsed.pathname === "/dashboard" || parsed.pathname.startsWith("/dashboard/");
    if (!dashboardPath && parsed.pathname !== "/") return "/dashboard";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/dashboard";
  }
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function dashboardCookieValue(rawHeader: string | undefined, name: string): string {
  if (!rawHeader) return "";
  for (const part of rawHeader.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) {
      try {
        return decodeURIComponent(rest.join("="));
      } catch {
        return "";
      }
    }
  }
  return "";
}

export function serializeDashboardCookie(
  name: string,
  value: string,
  options: { maxAgeSeconds?: number; secure?: boolean; path?: string; sameSite?: "Lax" | "Strict"; httpOnly?: boolean } = {},
): string {
  const parts = [`${name}=${encodeURIComponent(value)}`, `Path=${options.path ?? "/"}`];
  if (typeof options.maxAgeSeconds === "number") parts.push(`Max-Age=${Math.max(0, Math.trunc(options.maxAgeSeconds))}`);
  if (options.httpOnly !== false) parts.push("HttpOnly");
  parts.push(`SameSite=${options.sameSite ?? "Lax"}`);
  if (options.secure) parts.push("Secure");
  return parts.join("; ");
}

export function createDashboardSessionId(): string {
  return base64Url(randomBytes(32));
}

export function dashboardSessionHash(sessionId: string): string {
  return createHash("sha256").update(sessionId).digest("hex");
}

export function deriveDashboardSessionKey(secret: string): Buffer {
  return createHash("sha256").update(secret).digest();
}

export function encryptDashboardSessionText(value: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return `${base64Url(iv)}.${base64Url(tag)}.${base64Url(encrypted)}`;
}

export function decryptDashboardSessionText(value: string, key: Buffer): string {
  const [ivRaw, tagRaw, encryptedRaw] = value.split(".");
  if (!ivRaw || !tagRaw || !encryptedRaw) throw new Error("invalid_encrypted_session_value");
  const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(ivRaw, "base64url"));
  decipher.setAuthTag(Buffer.from(tagRaw, "base64url"));
  return Buffer.concat([
    decipher.update(Buffer.from(encryptedRaw, "base64url")),
    decipher.final(),
  ]).toString("utf8");
}

function signOAuthPayload(secret: string, payload: string): string {
  return base64Url(createHmac("sha256", secret).update(payload).digest());
}

export function issueDashboardOAuthState(
  secret: string,
  returnTo: unknown,
  secure: boolean,
  now = Date.now(),
): { state: string; setCookie: string } {
  const payload = base64Url(JSON.stringify({
    nonce: base64Url(randomBytes(24)),
    returnTo: safeDashboardReturnPath(returnTo),
    exp: now + DASHBOARD_OAUTH_STATE_LIFETIME_MS,
  }));
  const state = `${payload}.${signOAuthPayload(secret, payload)}`;
  return {
    state,
    setCookie: serializeDashboardCookie(DASHBOARD_OAUTH_COOKIE, state, {
      maxAgeSeconds: DASHBOARD_OAUTH_STATE_LIFETIME_MS / 1000,
      secure,
      sameSite: "Lax",
    }),
  };
}

export function validateDashboardOAuthState(
  secret: string,
  state: unknown,
  cookieHeader: string | undefined,
  now = Date.now(),
): { ok: true; returnTo: string } | { ok: false; reason: string } {
  const incoming = typeof state === "string" ? state.trim() : "";
  const stored = dashboardCookieValue(cookieHeader, DASHBOARD_OAUTH_COOKIE);
  if (!incoming || !stored || !constantTimeEqual(incoming, stored)) {
    return { ok: false, reason: "oauth_state_mismatch" };
  }
  const [payload, signature] = incoming.split(".");
  if (!payload || !signature || !constantTimeEqual(signature, signOAuthPayload(secret, payload))) {
    return { ok: false, reason: "oauth_state_invalid" };
  }
  try {
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as { exp?: number; returnTo?: string };
    if (!Number.isFinite(parsed.exp) || Number(parsed.exp) < now) {
      return { ok: false, reason: "oauth_state_expired" };
    }
    return { ok: true, returnTo: safeDashboardReturnPath(parsed.returnTo) };
  } catch {
    return { ok: false, reason: "oauth_state_invalid" };
  }
}
