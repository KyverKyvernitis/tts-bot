export const DASHBOARD_REFRESH_MARGIN_MS = 5 * 60 * 1000;

export function dashboardAccessExpiresAt(expiresInSeconds: number | null | undefined, now = Date.now()): number | null {
  return typeof expiresInSeconds === "number" && Number.isFinite(expiresInSeconds) && expiresInSeconds > 0
    ? now + expiresInSeconds * 1000
    : null;
}

export function dashboardSessionExpired(expiresAt: Date | number, now = Date.now()): boolean {
  const timestamp = expiresAt instanceof Date ? expiresAt.getTime() : expiresAt;
  return !Number.isFinite(timestamp) || timestamp <= now;
}

export function dashboardSessionNeedsRefresh(
  accessExpiresAt: Date | number | null | undefined,
  now = Date.now(),
  marginMs = DASHBOARD_REFRESH_MARGIN_MS,
): boolean {
  if (accessExpiresAt == null) return false;
  const timestamp = accessExpiresAt instanceof Date ? accessExpiresAt.getTime() : accessExpiresAt;
  return Number.isFinite(timestamp) && timestamp <= now + Math.max(0, marginMs);
}

export function dashboardNextRefreshToken(current: string, refreshed: string | null | undefined): string {
  return refreshed ?? current;
}
