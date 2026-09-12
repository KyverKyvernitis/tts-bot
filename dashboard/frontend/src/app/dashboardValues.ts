import type { DashboardFieldDefinition } from "../types/dashboard";

export function valuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

export function normalizeInputValue(field: DashboardFieldDefinition, raw: unknown): unknown {
  if (["role_multi", "string_list", "form_fields", "color_slots", "color_panel_layout"].includes(field.type)) return raw;
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "number") {
    if (raw === "" || raw === null || raw === undefined) return 0;
    const number = Number(raw);
    return Number.isFinite(number) ? number : 0;
  }
  if (field.type === "channel" || field.type === "role") {
    const match = String(raw ?? "").match(/\d{15,25}/);
    return match?.[0] || "";
  }
  return typeof raw === "string" ? raw : raw ?? "";
}
