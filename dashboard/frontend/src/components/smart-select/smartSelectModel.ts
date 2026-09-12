export interface SmartSelectOption {
  value: string;
  label: string;
  hint?: string;
  disabled?: boolean;
}

export interface SmartSelectRect {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
}

export interface SmartSelectPosition {
  left: number;
  top: number;
  width: number;
  placement: "above" | "below";
}

export function filterSmartSelectOptions(options: SmartSelectOption[], query: string): SmartSelectOption[] {
  const normalized = query.trim().toLocaleLowerCase("pt-BR");
  if (!normalized) return options;
  return options.filter((option) => `${option.label} ${option.hint || ""}`.toLocaleLowerCase("pt-BR").includes(normalized));
}

export function firstEnabledSmartSelectIndex(options: SmartSelectOption[]): number {
  return options.findIndex((option) => !option.disabled);
}

export function adjacentEnabledSmartSelectIndex(
  options: SmartSelectOption[],
  current: number,
  direction: -1 | 1,
): number {
  if (!options.length) return -1;
  let index = current;
  for (let attempts = 0; attempts < options.length; attempts += 1) {
    index += direction;
    if (index < 0 || index >= options.length) return current;
    if (!options[index]?.disabled) return index;
  }
  return current;
}

export function smartSelectEstimatedHeight(optionCount: number): number {
  return Math.min(420, Math.max(220, optionCount * 52 + (optionCount > 8 ? 64 : 0)));
}

export function smartSelectPosition(
  rect: SmartSelectRect,
  viewportWidth: number,
  viewportHeight: number,
  measuredHeight: number,
): SmartSelectPosition {
  const width = Math.max(220, rect.width);
  const left = Math.min(Math.max(8, rect.left), Math.max(8, viewportWidth - width - 8));
  const roomBelow = viewportHeight - rect.bottom - 8;
  const roomAbove = rect.top - 8;
  const placement = roomBelow < Math.min(measuredHeight, 260) && roomAbove > roomBelow ? "above" : "below";
  const top = placement === "above"
    ? Math.max(8, rect.top - measuredHeight - 7)
    : Math.min(viewportHeight - 8, rect.bottom + 7);
  return { left, top, width, placement };
}
