import { useEffect, useRef, useState, type MutableRefObject, type RefObject } from "react";
import {
  SIDEBAR_EDGE_GESTURE_MIN_X,
  SIDEBAR_MOBILE_BREAKPOINT,
  sidebarDragOffset,
  sidebarGestureAxis,
  sidebarGestureIsHorizontal,
  sidebarMaxGestureStartX,
  sidebarShouldClose,
  sidebarShouldOpen,
  type SidebarDrawerGestureMode,
} from "./sidebarGestureModel";

type DrawerPointer = {
  pointerId: number;
  mode: SidebarDrawerGestureMode;
  startX: number;
  startY: number;
  latestX: number;
  latestAt: number;
  velocityX: number;
  horizontal: boolean;
};

interface UseSidebarGestureEventsOptions {
  asideRef: RefObject<HTMLElement>;
  visualOpenRef: MutableRefObject<boolean>;
  gestureDisabled: boolean;
  setVisualOpen(open: boolean): void;
  onCloseMobile(): void;
  onOpenMobile(): void;
}

function isGestureBlockedTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest(
    "input, textarea, select, [contenteditable='true'], .osk-message-editor, .osk-account-layer, .osk-select-layer, [data-no-drawer-gesture]",
  ));
}

function hasBlockingOverlay() {
  return Boolean(document.querySelector(
    ".osk-account-layer[data-visible], .osk-select-layer[data-open], .osk-select-layer[data-visible], .osk-message-editor",
  ));
}

export function useSidebarGestureEvents(options: UseSidebarGestureEventsOptions) {
  const { asideRef, visualOpenRef, gestureDisabled, setVisualOpen, onCloseMobile, onOpenMobile } = options;
  const pointerRef = useRef<DrawerPointer | null>(null);
  const suppressClickUntilRef = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState(0);

  useEffect(() => {
    const drawerWidth = () => asideRef.current?.getBoundingClientRect().width || 280;
    const resetGesture = (settleOpen: boolean) => {
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
      setVisualOpen(settleOpen);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (window.innerWidth > SIDEBAR_MOBILE_BREAKPOINT || gestureDisabled) return;
      if (!event.isPrimary || event.pointerType === "mouse" || pointerRef.current) return;
      if (hasBlockingOverlay()) return;
      const open = visualOpenRef.current;
      if (!open) {
        if (isGestureBlockedTarget(event.target)) return;
        const maxStart = sidebarMaxGestureStartX(window.innerWidth);
        if (event.clientX < SIDEBAR_EDGE_GESTURE_MIN_X || event.clientX > maxStart) return;
      }
      pointerRef.current = {
        pointerId: event.pointerId,
        mode: open ? "closing" : "opening",
        startX: event.clientX,
        startY: event.clientY,
        latestX: event.clientX,
        latestAt: event.timeStamp,
        velocityX: 0,
        horizontal: false,
      };
    };
    const onPointerMove = (event: PointerEvent) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;
      if (!pointer.horizontal) {
        const axis = sidebarGestureAxis(pointer.mode, deltaX, deltaY);
        if (axis === "vertical") { resetGesture(pointer.mode === "closing"); return; }
        if (axis === "horizontal") {
          pointer.horizontal = true;
          setDragging(true);
          suppressClickUntilRef.current = performance.now() + 280;
        }
      }
      if (!pointer.horizontal) return;
      if (event.cancelable) event.preventDefault();
      const elapsed = Math.max(1, event.timeStamp - pointer.latestAt);
      pointer.velocityX = (event.clientX - pointer.latestX) / elapsed;
      pointer.latestX = event.clientX;
      pointer.latestAt = event.timeStamp;
      setDragOffset(sidebarDragOffset(pointer.mode, deltaX, drawerWidth()));
    };
    const finishGesture = (event: PointerEvent, cancelled = false) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;
      const width = drawerWidth();
      const horizontal = pointer.horizontal || sidebarGestureIsHorizontal(deltaX, deltaY);
      if (!horizontal || cancelled) { resetGesture(pointer.mode === "closing"); return; }
      suppressClickUntilRef.current = performance.now() + 300;
      if (pointer.mode === "opening") {
        const shouldOpen = sidebarShouldOpen(deltaX, pointer.velocityX, width);
        pointerRef.current = null;
        setDragging(false);
        setDragOffset(0);
        setVisualOpen(shouldOpen);
        if (shouldOpen) onOpenMobile();
        return;
      }
      const shouldClose = sidebarShouldClose(deltaX, pointer.velocityX, width);
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
      setVisualOpen(!shouldClose);
      if (shouldClose) onCloseMobile();
    };
    const onPointerUp = (event: PointerEvent) => finishGesture(event);
    const onPointerCancel = (event: PointerEvent) => finishGesture(event, true);
    const onClickCapture = (event: MouseEvent) => {
      if (performance.now() >= suppressClickUntilRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    };
    document.addEventListener("pointerdown", onPointerDown, { capture: true, passive: true });
    document.addEventListener("pointermove", onPointerMove, { capture: true, passive: false });
    document.addEventListener("pointerup", onPointerUp, { capture: true, passive: true });
    document.addEventListener("pointercancel", onPointerCancel, { capture: true, passive: true });
    document.addEventListener("click", onClickCapture, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("pointermove", onPointerMove, true);
      document.removeEventListener("pointerup", onPointerUp, true);
      document.removeEventListener("pointercancel", onPointerCancel, true);
      document.removeEventListener("click", onClickCapture, true);
    };
  }, [asideRef, gestureDisabled, onCloseMobile, onOpenMobile, setVisualOpen, visualOpenRef]);

  return { pointerRef, dragging, dragOffset, setDragging, setDragOffset };
}
