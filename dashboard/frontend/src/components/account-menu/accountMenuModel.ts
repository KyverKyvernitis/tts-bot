import type { DashboardUserPayload } from "../../types/dashboard";

export interface AccountMenuTriggerRect {
  bottom: number;
  right: number;
}

export function accountIdentityName(user: DashboardUserPayload): string {
  return user.global_name?.trim() || user.username?.trim() || "Conta";
}

export function accountMenuPosition(rect: AccountMenuTriggerRect, viewportWidth: number, viewportHeight: number) {
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
