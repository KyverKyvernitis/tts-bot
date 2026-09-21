import type { DashboardFieldDefinition } from "../types/dashboard";
import { valuesEqual } from "./dashboardValues";

export function dashboardFieldError(field: DashboardFieldDefinition, value: unknown): string | null {
  const text = String(value ?? "");
  if (field.maxLength && text.length > field.maxLength && typeof value === "string") return `Use até ${field.maxLength} caracteres.`;
  if (field.type === "number") {
    const number = Number(value);
    if (value === "" || value === null || value === undefined || !Number.isFinite(number)) return "Informe um número válido.";
    if (field.min !== undefined && number < field.min) return `O mínimo é ${field.min}.`;
    if (field.max !== undefined && number > field.max) return `O máximo é ${field.max}.`;
  }
  if (field.type === "url" && text.trim()) {
    let url = text.trim();
    if ((url.startsWith("<") && url.endsWith(">")) || (url.startsWith('"') && url.endsWith('"')) || (url.startsWith("'") && url.endsWith("'"))) url = url.slice(1, -1).trim();
    try { if (!["https:", "http:"].includes(new URL(url.replace(/&amp;/gi, "&")).protocol)) return "Use um endereço HTTP ou HTTPS."; }
    catch { return "Informe um endereço completo, como https://exemplo.com/imagem.png."; }
  }
  return null;
}

export function dashboardChangedFieldErrors(fields: DashboardFieldDefinition[], draft: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(fields.flatMap(field => { const error = dashboardFieldError(field, draft[field.id]); return error ? [[field.id, error]] : []; }));
}

export function reconcileSavedDraft(submitted: Record<string, unknown>, current: Record<string, unknown>, saved: Record<string, unknown>): Record<string, unknown> {
  const next = { ...saved };
  for (const [id, value] of Object.entries(current)) if (!valuesEqual(value, submitted[id])) next[id] = value;
  return next;
}
