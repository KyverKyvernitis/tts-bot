import { useCallback, useEffect, useRef, useState } from "react";
import { sidebarDrawerProgress } from "./sidebarGestureModel";
import { useSidebarAccessibility } from "./useSidebarAccessibility";
import { useSidebarGestureEvents } from "./useSidebarGestureEvents";

interface UseSidebarDrawerOptions {
  mobileOpen: boolean;
  gestureDisabled: boolean;
  onCloseMobile(): void;
  onOpenMobile(): void;
}

export function useSidebarDrawer({ mobileOpen, gestureDisabled, onCloseMobile, onOpenMobile }: UseSidebarDrawerOptions) {
  const asideRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const visualOpenRef = useRef(mobileOpen);
  const [visualOpen, setVisualOpenState] = useState(mobileOpen);

  const setVisualOpen = useCallback((open: boolean) => {
    visualOpenRef.current = open;
    setVisualOpenState(open);
  }, []);

  const { pointerRef, dragging, dragOffset, setDragging, setDragOffset } = useSidebarGestureEvents({
    asideRef,
    visualOpenRef,
    gestureDisabled,
    setVisualOpen,
    onCloseMobile,
    onOpenMobile,
  });

  useEffect(() => {
    setVisualOpen(mobileOpen);
    if (!mobileOpen) {
      pointerRef.current = null;
      setDragging(false);
      setDragOffset(0);
    }
  }, [mobileOpen, pointerRef, setDragOffset, setDragging, setVisualOpen]);

  useSidebarAccessibility({ asideRef, closeRef, pointerRef, visualOpen, dragging, setVisualOpen, onCloseMobile });

  const close = useCallback(() => {
    setVisualOpen(false);
    onCloseMobile();
  }, [onCloseMobile, setVisualOpen]);

  const width = asideRef.current?.getBoundingClientRect().width || 280;
  const progress = sidebarDrawerProgress(dragging, dragOffset, width, visualOpen);
  return { asideRef, closeRef, visualOpen, dragging, dragOffset, progress, close };
}
