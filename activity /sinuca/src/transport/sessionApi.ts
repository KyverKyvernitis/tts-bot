import { DashboardHttpError, fetchJsonFromCandidate, resolveTokenCandidates } from "./httpClient";

export type OAuthExchangeResult = {
  ok: boolean;
  accessToken: string | null;
  error: string | null;
  detail: string | null;
};

type TokenResponse = {
  ok?: boolean;
  access_token?: string;
  accessToken?: string;
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

export async function exchangeDiscordTokenRequest(code: string, redirectUri?: string | null): Promise<OAuthExchangeResult> {
  const attempts: string[] = [];
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

  for (const variant of variants) {
    try {
      const parsed = await fetchJsonFromCandidate<TokenResponse>(variant.url, variant.init, 5000);
      const accessToken = parsed.access_token ?? parsed.accessToken ?? null;
      if (typeof accessToken === "string" && accessToken.trim()) {
        return { ok: true, accessToken, error: null, detail: `ok:${variant.label}` };
      }
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
    error: reason,
    detail: attempts.length ? attempts.slice(0, 8).join(" | ") : null,
  };
}
