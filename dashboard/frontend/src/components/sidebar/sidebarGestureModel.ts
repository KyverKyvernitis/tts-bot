export type SidebarDrawerGestureMode = "opening" | "closing";

export const SIDEBAR_MOBILE_BREAKPOINT = 980;
export const SIDEBAR_EDGE_GESTURE_MIN_X = 16;
export const SIDEBAR_EDGE_GESTURE_MAX_X = 144;
export const SIDEBAR_AXIS_LOCK_DISTANCE = 8;
export const SIDEBAR_AXIS_RATIO = 1.12;
export const SIDEBAR_OPEN_DISTANCE = 64;
export const SIDEBAR_CLOSE_DISTANCE = 72;
export const SIDEBAR_OPEN_VELOCITY = 0.45;
export const SIDEBAR_CLOSE_VELOCITY = -0.45;

export function sidebarMaxGestureStartX(viewportWidth: number) {
  return Math.min(SIDEBAR_EDGE_GESTURE_MAX_X, viewportWidth * 0.32);
}

export function sidebarGestureAxis(
  mode: SidebarDrawerGestureMode,
  deltaX: number,
  deltaY: number,
): "pending" | "horizontal" | "vertical" {
  const intendedDelta = mode === "opening" ? deltaX : -deltaX;
  if (Math.abs(deltaY) >= SIDEBAR_AXIS_LOCK_DISTANCE && Math.abs(deltaY) > Math.abs(deltaX) * SIDEBAR_AXIS_RATIO) {
    return "vertical";
  }
  if (intendedDelta >= SIDEBAR_AXIS_LOCK_DISTANCE && Math.abs(deltaX) > Math.abs(deltaY) * SIDEBAR_AXIS_RATIO) {
    return "horizontal";
  }
  return "pending";
}

export function sidebarGestureIsHorizontal(deltaX: number, deltaY: number) {
  return Math.abs(deltaX) >= 42 && Math.abs(deltaX) > Math.abs(deltaY) * SIDEBAR_AXIS_RATIO;
}

export function sidebarDragOffset(mode: SidebarDrawerGestureMode, deltaX: number, width: number) {
  return mode === "opening"
    ? Math.max(-width, Math.min(0, -width + Math.max(0, deltaX)))
    : Math.max(-width, Math.min(0, Math.min(0, deltaX)));
}

export function sidebarShouldOpen(deltaX: number, velocityX: number, width: number) {
  const openedDistance = Math.max(0, deltaX);
  return openedDistance >= Math.min(SIDEBAR_OPEN_DISTANCE, width * 0.24)
    || velocityX >= SIDEBAR_OPEN_VELOCITY;
}

export function sidebarShouldClose(deltaX: number, velocityX: number, width: number) {
  const closedDistance = Math.max(0, -deltaX);
  return closedDistance >= Math.min(SIDEBAR_CLOSE_DISTANCE, width * 0.24)
    || velocityX <= SIDEBAR_CLOSE_VELOCITY;
}

export function sidebarDrawerProgress(dragging: boolean, dragOffset: number, width: number, visualOpen: boolean) {
  return dragging
    ? Math.max(0, Math.min(1, 1 - Math.abs(dragOffset) / width))
    : visualOpen ? 1 : 0;
}
