import { defaultColorPanelLayout, defaultColorSlots } from "./dashboardColorRoleDefaults.js";
import { isPlainObject } from "./dashboardObjectUtils.js";
import { snowflakeFromRaw, serializeSnowflake } from "./dashboardSnowflakes.js";

export function normalizeDashboardColor(raw: unknown, fallback = ""): string {
  const text = String(raw ?? "").trim();
  if (!text) return fallback;
  const normalized = text.startsWith("#") ? text : `#${text}`;
  return /^#[0-9a-fA-F]{6}$/.test(normalized) ? normalized.toUpperCase() : fallback;
}

export function normalizeColorPanelLayout(raw: unknown): Array<{ id: string; slots: number[] }> {
  const source = Array.isArray(raw) ? raw : defaultColorPanelLayout();
  const used = new Set<number>();
  const panels: Array<{ id: string; slots: number[] }> = [];
  for (const [index, rawPanel] of source.slice(0, 3).entries()) {
    const panel = isPlainObject(rawPanel) ? rawPanel : {};
    const slots: number[] = [];
    for (const rawSlot of Array.isArray(panel.slots) ? panel.slots : []) {
      const number = Math.trunc(Number(rawSlot));
      if (number < 1 || number > 30 || used.has(number)) continue;
      used.add(number);
      slots.push(number);
      if (slots.length >= 10) break;
    }
    if (!slots.length) continue;
    const requestedId = String(panel.id || "").trim().slice(0, 80);
    let id = requestedId && !panels.some((item) => item.id === requestedId) ? requestedId : `panel-${index + 1}`;
    let suffix = 2;
    while (panels.some((item) => item.id === id)) {
      id = `panel-${index + 1}-${suffix}`;
      suffix += 1;
    }
    panels.push({ id, slots });
  }
  return panels.length ? panels : [{ id: "panel-1", slots: [1] }];
}

export function normalizeColorSlots(raw: unknown): Record<string, unknown> {
  const defaults = defaultColorSlots();
  const source = isPlainObject(raw) ? raw : {};
  const result: Record<string, unknown> = {};
  for (const [number, defaultRaw] of Object.entries(defaults)) {
    const base = defaultRaw as Record<string, unknown>;
    const item = isPlainObject(source[number]) ? source[number] as Record<string, unknown> : {};
    const name = String(item.name || base.name).trim().slice(0, 80) || String(base.name);
    const roleId = snowflakeFromRaw(item.role_id);
    const hasRoleId = roleId.toString() !== "0";
    let textHex = normalizeDashboardColor(item.text_hex, String(base.text_hex)).toLowerCase();
    let roleHex = normalizeDashboardColor(item.role_hex, String(base.role_hex)).toLowerCase();
    const placeholderWhite = !hasRoleId
      && Number(number) !== 30
      && textHex === "#ffffff"
      && roleHex === "#ffffff";
    if (placeholderWhite) {
      textHex = String(base.text_hex).toLowerCase();
      roleHex = String(base.role_hex).toLowerCase();
    }
    result[number] = {
      number: Number(number),
      name,
      text_hex: textHex,
      role_hex: roleHex,
      role_id: roleId,
      role_name: String(item.role_name || name).trim().slice(0, 100) || name,
      managed: Boolean(item.managed),
    };
  }
  return result;
}

export function serializeColorSlots(raw: unknown): Record<string, unknown> {
  const source = isPlainObject(raw) ? raw : {};
  return Object.fromEntries(Object.entries(source).map(([key, item]) => {
    const value = isPlainObject(item) ? { ...item } : {};
    value.role_id = serializeSnowflake(value.role_id);
    return [key, value];
  }));
}
