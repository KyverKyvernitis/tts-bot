import { fetchWithTimeout, resolveApiCandidates } from "./httpClient";

export type OAuthExchangeResult = {
  ok: boolean;
  accessToken: string | null;
  error: string | null;
  detail: string | null;
};

export async function exchangeDiscordTokenRequest(code: string): Promise<OAuthExchangeResult> {
  const baseCandidates = resolveApiCandidates("/token");
  const attempts: string[] = [];
  const requestVariants: Array<{ label: string; url: string; init: RequestInit }> = [];

  for (const baseUrl of baseCandidates) {
    requestVariants.push({
      label: `POST_JSON:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ code }),
      },
    });
    requestVariants.push({
      label: `POST_FORM:${baseUrl}`,
      url: baseUrl,
      init: {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        credentials: "same-origin",
        body: new URLSearchParams({ code }).toString(),
      },
    });
    requestVariants.push({
      label: `GET_QUERY:${baseUrl}`,
      url: `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}code=${encodeURIComponent(code)}`,
      init: {
        method: "GET",
        credentials: "same-origin",
      },
    });
  }

  for (const variant of requestVariants) {
    try {
      const response = await fetchWithTimeout(variant.url, variant.init, 4000);
      const raw = await response.text();
      let parsed: { access_token?: string; error?: string; detail?: string } | null = null;
      try {
        parsed = raw ? JSON.parse(raw) as { access_token?: string; error?: string; detail?: string } : null;
      } catch {
        parsed = null;
      }

      if (response.ok && typeof parsed?.access_token === "string" && parsed.access_token) {
        return {
          ok: true,
          accessToken: parsed.access_token,
          error: null,
          detail: `http_ok:${variant.label}:${response.status}`,
        };
      }

      const detail = parsed?.error ?? parsed?.detail ?? (raw.slice(0, 180) || "empty");
      attempts.push(`${variant.label}:${response.status}:${detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown";
      attempts.push(`${variant.label}:exception:${message}`);
    }
  }

  return {
    ok: false,
    accessToken: null,
    error: "http_exchange_failed",
    detail: attempts.length ? attempts.join(" | ") : null,
  };
}
