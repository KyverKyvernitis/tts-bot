export function normalizePreviewUrl(value: unknown): string {
  if (typeof value !== "string") return "";
  let normalized = value
    .replace(/[\u200B-\u200D\u2060\uFEFF]/g, "")
    .replace(/\\u0026/gi, "&")
    .replace(/\\u003d/gi, "=")
    .replace(/\\\//g, "/")
    .trim();
  if (!normalized) return "";
  const markdownLink = normalized.match(/^\[[^\]]*\]\((https?:\/\/.+)\)$/is);
  if (markdownLink) normalized = markdownLink[1].trim();
  if ((normalized.startsWith("<") && normalized.endsWith(">"))
    || (normalized.startsWith('"') && normalized.endsWith('"'))
    || (normalized.startsWith("'") && normalized.endsWith("'"))) {
    normalized = normalized.slice(1, -1).trim();
  }
  normalized = normalized.replace(/&amp;/gi, "&");
  return normalized;
}

export function isValidPreviewUrl(value: unknown): value is string {
  const normalized = normalizePreviewUrl(value);
  if (!normalized) return false;
  try {
    const parsed = new URL(normalized);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

export interface DiscordAttachmentUrlInfo {
  expiresAt: number | null;
  expired: boolean;
}

function parsedDiscordAttachmentUrl(value: unknown): URL | null {
  const normalized = normalizePreviewUrl(value);
  if (!isValidPreviewUrl(normalized)) return null;
  try {
    const parsed = new URL(normalized);
    const host = parsed.hostname.toLowerCase();
    if (host !== "cdn.discordapp.com" && host !== "media.discordapp.net") return null;
    if (!/^\/attachments\/\d{15,24}\/\d{15,24}\/[^/]+$/.test(parsed.pathname)) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function discordAttachmentUrlInfo(value: unknown, now = Date.now()): DiscordAttachmentUrlInfo | null {
  const parsed = parsedDiscordAttachmentUrl(value);
  if (!parsed) return null;
  const expiresHex = parsed.searchParams.get("ex") || "";
  const expiresSeconds = /^[a-f\d]{8,16}$/i.test(expiresHex) ? Number.parseInt(expiresHex, 16) : Number.NaN;
  const expiresAt = Number.isFinite(expiresSeconds) ? expiresSeconds * 1000 : null;
  return { expiresAt, expired: expiresAt !== null && expiresAt <= now };
}

export function discordAttachmentPreviewProxyUrl(value: unknown): string {
  const parsed = parsedDiscordAttachmentUrl(value);
  return parsed ? `/api/dashboard/media-preview?url=${encodeURIComponent(parsed.toString())}` : "";
}

export function previewImageCandidates(value: unknown): string[] {
  const normalized = normalizePreviewUrl(value);
  if (!isValidPreviewUrl(normalized)) return [];

  const candidates = [normalized];
  try {
    const parsed = new URL(normalized);
    const host = parsed.hostname.toLowerCase();
    if (host === "cdn.discordapp.com") {
      parsed.hostname = "media.discordapp.net";
      candidates.push(parsed.toString());
    } else if (host === "media.discordapp.net") {
      parsed.hostname = "cdn.discordapp.com";
      candidates.push(parsed.toString());
    }
    const proxyUrl = discordAttachmentPreviewProxyUrl(normalized);
    if (proxyUrl) candidates.push(proxyUrl);
  } catch {
    // A validação acima já elimina URLs inválidas; mantenha apenas a original.
  }

  return Array.from(new Set(candidates));
}
