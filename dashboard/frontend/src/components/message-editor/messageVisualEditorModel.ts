import type { DashboardFieldDefinition } from "../../types/dashboard";

export function messageVisualValuesEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

export function isMessagePreviewImageUrlField(field: DashboardFieldDefinition): boolean {
  return field.type === "url" && /(?:^|\.)(?:image|thumbnail|media|avatar|author_icon|footer_icon)_url$/i.test(field.id);
}

export function contextualMessageOptions(
  field: DashboardFieldDefinition,
  currentValue: string,
): NonNullable<DashboardFieldDefinition["options"]> {
  const configured = field.options ?? [];
  if (!currentValue || configured.some((option) => option.value === currentValue)) return configured;
  return [{ value: currentValue, label: `${currentValue} — valor atual` }, ...configured];
}
