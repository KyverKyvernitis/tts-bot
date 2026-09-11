import { Long } from "mongodb";

export function snowflakeToLong(value: string): Long {
  const text = String(value ?? "").trim();
  if (!/^\d{1,25}$/.test(text)) return Long.ZERO;
  try { return Long.fromString(text, false); } catch { return Long.ZERO; }
}
export function snowflakeFromRaw(raw: unknown): Long {
  const match = String(raw ?? "").trim().match(/\d{15,25}/);
  return match ? snowflakeToLong(match[0]) : Long.ZERO;
}
export function isLongLike(value: unknown): value is Long {
  return value instanceof Long || (typeof value === "object" && value !== null && typeof (value as { low?: unknown }).low === "number" && typeof (value as { high?: unknown }).high === "number");
}
export function serializeSnowflake(value: unknown): string {
  if (isLongLike(value)) { const text = value.toString(); return text === "0" ? "" : text; }
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(Math.trunc(value)) : "";
  const text = String(value ?? "").trim();
  return text === "0" ? "" : text;
}
