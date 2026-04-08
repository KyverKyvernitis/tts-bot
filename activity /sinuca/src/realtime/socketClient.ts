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
  const payloadType = typeof payload === "object" && payload !== null && "type" in (payload as Record<string, unknown>)
    ? String((payload as Record<string, unknown>).type ?? "unknown")
    : "unknown";
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    console.warn("[sinuca-ws-send-skipped]", JSON.stringify({ payloadType, readyState: socket?.readyState ?? null }));
    return false;
  }
  if (payloadType === "take_shot" || payloadType === "sync_aim" || payloadType === "init_context") {
    console.log("[sinuca-ws-send]", JSON.stringify({ payloadType, readyState: socket.readyState }));
  }
  socket.send(JSON.stringify(payload));
  return true;
}
