export interface DiscordJsonResponse<T> {
  ok: boolean;
  status: number;
  data: T | null;
}

async function parseDiscordResponse<T>(response: Response): Promise<DiscordJsonResponse<T>> {
  const text = await response.text();
  let data: T | null = null;
  try {
    data = text ? JSON.parse(text) as T : null;
  } catch {
    data = null;
  }
  return { ok: response.ok, status: response.status, data };
}

export async function fetchDiscordJson<T>(url: string, authorization: string): Promise<DiscordJsonResponse<T>> {
  try {
    const response = await fetch(url, { headers: { Authorization: authorization } });
    return await parseDiscordResponse<T>(response);
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

export async function fetchDiscordPublicJson<T>(url: string): Promise<DiscordJsonResponse<T>> {
  try {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        "User-Agent": "OsakaDashboard/2.0",
      },
    });
    return await parseDiscordResponse<T>(response);
  } catch {
    return { ok: false, status: 0, data: null };
  }
}
