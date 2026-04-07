import type { IncomingMessage } from "http";
import type { SessionContextPayload } from "../messages.js";

export function normalizeIntString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const raw = String(value).trim();
  return raw ? raw : null;
}

export function firstString(value: unknown): string | null {
  if (Array.isArray(value)) {
    return firstString(value[0]);
  }
  return typeof value === "string" ? value : null;
}

export function booleanish(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return fallback;
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off"].includes(normalized)) return false;
  }
  return fallback;
}

export function normalizeRoomMode(value: unknown, fallbackGuildId?: string | null): "server" | "casual" {
  if (value === "server") return "server";
  if (value === "casual") return "casual";
  return fallbackGuildId ? "server" : "casual";
}

function parseQuerySession(urlValue: string | undefined | null): Partial<SessionContextPayload> {
  if (!urlValue) return {};
  try {
    const url = new URL(urlValue, "https://sinuca.local");
    return {
      userId: normalizeIntString(url.searchParams.get("user_id") ?? url.searchParams.get("userId")),
      displayName: normalizeIntString(url.searchParams.get("display_name") ?? url.searchParams.get("displayName")),
      guildId: normalizeIntString(url.searchParams.get("guild_id") ?? url.searchParams.get("guildId")),
      channelId: normalizeIntString(url.searchParams.get("channel_id") ?? url.searchParams.get("channelId")),
      instanceId: normalizeIntString(url.searchParams.get("instance_id") ?? url.searchParams.get("instanceId")),
    };
  } catch {
    return {};
  }
}

function mergeNullableSession(base: SessionContextPayload, incoming: Partial<SessionContextPayload>): SessionContextPayload {
  return {
    userId: incoming.userId ?? base.userId,
    displayName: incoming.displayName ?? base.displayName,
    guildId: incoming.guildId ?? base.guildId,
    channelId: incoming.channelId ?? base.channelId,
    instanceId: incoming.instanceId ?? base.instanceId,
  };
}

function decodeProxyPayload(req: IncomingMessage): SessionContextPayload {
  const encoded = req.headers["x-discord-proxy-payload"];
  const result: SessionContextPayload = { userId: null, displayName: null, guildId: null, channelId: null, instanceId: null };
  if (!encoded || Array.isArray(encoded)) return result;
  try {
    const decoded = JSON.parse(Buffer.from(encoded, "base64").toString("utf-8")) as Record<string, any>;
    const user = decoded.user ?? decoded.member?.user ?? null;
    result.userId = normalizeIntString(decoded.user_id ?? decoded.userId ?? decoded.discord_user_id ?? user?.id ?? (Array.isArray(decoded.users) ? decoded.users[0] : null));
    result.displayName = normalizeIntString(decoded.display_name ?? decoded.displayName ?? decoded.member?.nick ?? user?.global_name ?? user?.username) ?? null;
    result.guildId = normalizeIntString(decoded.guild_id ?? decoded.guildId ?? decoded.location?.guild_id);
    result.channelId = normalizeIntString(decoded.channel_id ?? decoded.channelId ?? decoded.location?.channel_id);
    result.instanceId = normalizeIntString(decoded.instance_id ?? decoded.instanceId);
  } catch {
    // ignore malformed proxy payloads and fall back to client hints
  }
  return result;
}

export function resolveRequestSession(req: IncomingMessage): SessionContextPayload & { sessionSource: string } {
  const fromProxy = decodeProxyPayload(req);
  const fromPath = parseQuerySession(req.url ?? null);
  const fromReferer = parseQuerySession(typeof req.headers.referer === "string" ? req.headers.referer : null);
  const merged = mergeNullableSession(mergeNullableSession(fromProxy, fromPath), fromReferer);
  const sessionSource = fromProxy.userId || fromProxy.guildId || fromProxy.channelId || fromProxy.instanceId
    ? "proxy"
    : (fromPath.guildId || fromPath.channelId || fromPath.instanceId || fromReferer.guildId || fromReferer.channelId || fromReferer.instanceId)
      ? "url_hint"
      : "empty";
  return { ...merged, sessionSource };
}

export function mergeSession(base: SessionContextPayload, incoming: SessionContextPayload): SessionContextPayload {
  return {
    userId: incoming.userId ?? base.userId,
    displayName: incoming.displayName ?? base.displayName,
    guildId: incoming.guildId ?? base.guildId,
    channelId: incoming.channelId ?? base.channelId,
    instanceId: incoming.instanceId ?? base.instanceId,
  };
}

export function mergeWithSession<T extends Record<string, any>>(payload: T, session: SessionContextPayload): T {
  return {
    ...payload,
    guildId: payload.guildId ?? session.guildId,
    channelId: payload.channelId ?? session.channelId,
    userId: payload.userId ?? session.userId,
    displayName: payload.displayName ?? session.displayName,
    instanceId: payload.instanceId ?? session.instanceId,
  };
}
