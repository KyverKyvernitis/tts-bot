const DEFAULT_PUBLIC_HOST = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || "osakaagiota.duckdns.org";

export function resolveSocketUrl() {
  const configured = (import.meta.env.VITE_SINUCA_WS_URL as string | undefined)?.trim();
  if (configured) {
    const url = new URL(configured, window.location.origin);
    if (!url.search && window.location.search) url.search = window.location.search;
    return url.toString();
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const relativeSocketUrl = new URL(`/ws${window.location.search ?? ""}`, `${protocol}://${window.location.host}`);

  const configuredPublicHost = (import.meta.env.VITE_SINUCA_PUBLIC_HOST as string | undefined)?.trim() || DEFAULT_PUBLIC_HOST;
  if (configuredPublicHost) {
    const host = configuredPublicHost.replace(/^https?:\/\//i, "").replace(/\/$/, "");
    if (host && host !== window.location.host) {
      return relativeSocketUrl.toString();
    }
  }

  return relativeSocketUrl.toString();
}

export function sendSocketMessage(socket: WebSocket | null | undefined, payload: object) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  socket.send(JSON.stringify(payload));
  return true;
}
