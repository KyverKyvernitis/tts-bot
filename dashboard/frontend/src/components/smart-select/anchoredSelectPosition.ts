import type { SmartSelectRect } from './smartSelectModel';

interface SelectViewport { left: number; top: number; width: number; height: number }

export function anchoredSelectPosition(rect: SmartSelectRect, viewport: SelectViewport, measuredHeight: number) {
  const edge = 8;
  const gap = 7;
  const width = Math.min(Math.max(240, rect.width), Math.max(0, viewport.width - edge * 2));
  const left = Math.min(Math.max(viewport.left + edge, rect.left), viewport.left + viewport.width - width - edge);
  const below = Math.max(0, viewport.top + viewport.height - rect.bottom - gap - edge);
  const above = Math.max(0, rect.top - viewport.top - gap - edge);
  const desiredHeight = Math.min(measuredHeight, 420);
  const placement = below < desiredHeight && above > below ? 'above' as const : 'below' as const;
  const maxHeight = Math.min(420, placement === 'above' ? above : below);
  const height = Math.min(desiredHeight, maxHeight);
  const top = placement === 'above' ? rect.top - gap - height : rect.bottom + gap;
  return {left, top, width, maxHeight, placement};
}
