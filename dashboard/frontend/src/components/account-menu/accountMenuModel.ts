import type { DashboardUserPayload } from "../../types/dashboard";

export interface AccountMenuTriggerRect {
  bottom: number;
  right: number;
  top?: number;
}

export function accountIdentityName(user: DashboardUserPayload): string {
  return user.global_name?.trim() || user.username?.trim() || "Conta";
}

export function accountMenuPosition(
  rect: AccountMenuTriggerRect,
  viewportWidth: number,
  viewportHeight: number,
  options?: { menuHeight: number; menuWidth: number; margin: number },
) {
  if (options) {
    const margin = options.margin;
    const width = Math.min(options.menuWidth, Math.max(1, viewportWidth - margin * 2));
    const height = Math.min(options.menuHeight, Math.max(1, viewportHeight - margin * 2));
    const below = viewportHeight - rect.bottom - margin - 8;
    const above = (rect.top ?? rect.bottom) - margin - 8;
    const openAbove = below < height && above > below;
    const top = Math.min(
      Math.max(margin, viewportHeight - margin - 1),
      Math.max(margin, openAbove ? (rect.top ?? rect.bottom) - height - 8 : rect.bottom + 8),
    );
    return {
      top: Math.round(top),
      right: Math.round(Math.max(margin, Math.min(viewportWidth - width - margin, viewportWidth - rect.right))),
      maxHeight: Math.max(1, Math.floor(viewportHeight - top - margin)),
    };
  }
  return {
    top: Math.round(rect.bottom + 8),
    right: Math.max(8, Math.round(viewportWidth - rect.right)),
    maxHeight: Math.max(152, Math.floor(viewportHeight - rect.bottom - 16)),
  };
}

export function adjacentAccountMenuIndex(current: number, itemCount: number, direction: -1 | 1): number {
  if (itemCount <= 0) return -1;
  if (current < 0) return direction === 1 ? 0 : itemCount - 1;
  return (current + direction + itemCount) % itemCount;
}
