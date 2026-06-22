import { DashboardHttpError, fetchJsonFromCandidate, resolveTokenCandidates } from "./httpClient";

export type OAuthExchangeResult = {
  ok: boolean;
  accessToken: string | null;
  refreshToken: string | null;
  expiresIn: number | null;
  expiresAt: number | null;
  error: string | null;
  detail: string | null;
};

type TokenResponse = {
  ok?: boolean;
  access_token?: string;
  accessToken?: string;
  refresh_token?: string;
  refreshToken?: string;
  expires_in?: number | string;
  expiresIn?: number | string;
  error?: string;
  detail?: string;
};

function compactAttempt(error: unknown) {
  if (error instanceof DashboardHttpError) {
    if (error.code === "html_instead_of_json") return `${error.status || "sem_status"}:html_frontend`;
    return `${error.status || "sem_status"}:${error.code}:${error.message.slice(0, 80)}`;
  }
  return error instanceof Error ? error.message.slice(0, 120) : String(error).slice(0, 120);
}

function normalizeTokenResponse(parsed: TokenResponse, label: string): OAuthExchangeResult | null {
  const accessToken = parsed.access_token ?? parsed.accessToken ?? null;
  if (typeof accessToken !== "string" || !accessToken.trim()) return null;

  const refreshToken = parsed.refresh_token ?? parsed.refreshToken ?? null;
  const rawExpiresIn = parsed.expires_in ?? parsed.expiresIn ?? null;
  const expiresIn = typeof rawExpiresIn === "number"
    ? rawExpiresIn
    : (typeof rawExpiresIn === "string" && rawExpiresIn.trim() ? Number(rawExpiresIn) : null);
  const normalizedExpiresIn = typeof expiresIn === "number" && Number.isFinite(expiresIn) && expiresIn > 0 ? expiresIn : null;

  return {
    ok: true,
    accessToken: accessToken.trim(),
    refreshToken: typeof refreshToken === "string" && refreshToken.trim() ? refreshToken.trim() : null,
    expiresIn: normalizedExpiresIn,
    expiresAt: normalizedExpiresIn ? Date.now() + normalizedExpiresIn * 1000 : null,
    error: null,
    detail: `ok:${label}`,
  };
}

async function requestDiscordToken(variants: Array<{ label: string; url: string; init: RequestInit }>): Promise<OAuthExchangeResult> {
  const attempts: string[] = [];

  for (const variant of variants) {
    try {
      const parsed = await fetchJsonFromCandidate<TokenResponse>(variant.url, variant.init, 5000);
      const normalized = normalizeTokenResponse(parsed, variant.label);
      if (normalized) return normalized;
      attempts.push(`${variant.label}:sem_access_token:${parsed.error ?? parsed.detail ?? "resposta sem token"}`);
    } catch (error) {
      attempts.push(`${variant.label}:${compactAttempt(error)}`);
    }
  }

  const htmlHits = attempts.filter((item) => item.includes("html_frontend")).length;
  const reason = htmlHits >= Math.max(1, Math.floor(attempts.length / 2))
    ? "api_proxy_returning_frontend_html"
    : "http_exchange_failed";

  return {
    ok: false,
    accessToken: null,
    refreshToken: null,
    expiresIn: null,
    expiresAt: null,
    error: reason,
    detail: attempts.length ? attempts.slice(0, 8).join(" | ") : null,
  };
}

export async function exchangeDiscordTokenRequest(code: string, redirectUri?: string | null): Promise<OAuthExchangeResult> {
  const candidates = resolveTokenCandidates();
  const variants = candidates.flatMap((url) => ([
    {
      label: `json:${url}`,
      url,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin" as RequestCredentials,
        body: JSON.stringify({ code, redirect_uri: redirectUri || undefined }),
      },
    },
    {
      label: `form:${url}`,
      url,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        credentials: "same-origin" as RequestCredentials,
        body: new URLSearchParams({ code, ...(redirectUri ? { redirect_uri: redirectUri } : {}) }).toString(),
      },
    },
  ]));

  return await requestDiscordToken(variants);
}

export async function refreshDiscordTokenRequest(refreshToken: string): Promise<OAuthExchangeResult> {
  const token = refreshToken.trim();
  if (!token) {
    return {
      ok: false,
      accessToken: null,
      refreshToken: null,
      expiresIn: null,
      expiresAt: null,
      error: "missing_refresh_token",
      detail: null,
    };
  }

  const candidates = resolveTokenCandidates();
  const variants = candidates.flatMap((url) => ([
    {
      label: `refresh-json:${url}`,
      url,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin" as RequestCredentials,
        body: JSON.stringify({ grant_type: "refresh_token", refresh_token: token }),
      },
    },
    {
      label: `refresh-form:${url}`,
      url,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        credentials: "same-origin" as RequestCredentials,
        body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: token }).toString(),
      },
    },
  ]));

  return await requestDiscordToken(variants);
}
