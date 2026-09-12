import { useEffect, type MutableRefObject, type RefObject } from "react";
import { SIDEBAR_MOBILE_BREAKPOINT } from "./sidebarGestureModel";

interface UseSidebarAccessibilityOptions {
  asideRef: RefObject<HTMLElement>;
  closeRef: RefObject<HTMLButtonElement>;
  pointerRef: MutableRefObject<unknown | null>;
  visualOpen: boolean;
  dragging: boolean;
  setVisualOpen(open: boolean): void;
  onCloseMobile(): void;
}

export function useSidebarAccessibility(options: UseSidebarAccessibilityOptions): void {
  const { asideRef, closeRef, pointerRef, visualOpen, dragging, setVisualOpen, onCloseMobile } = options;

  useEffect(() => {
    const aside = asideRef.current;
    if (!aside) return;
    const syncAccessibility = () => {
      const mobile = window.innerWidth <= SIDEBAR_MOBILE_BREAKPOINT;
      const hidden = mobile && !visualOpen && !pointerRef.current;
      if (hidden) {
        aside.setAttribute("inert", "");
        aside.setAttribute("aria-hidden", "true");
      } else {
        aside.removeAttribute("inert");
        aside.removeAttribute("aria-hidden");
      }
    };
    syncAccessibility();
    window.addEventListener("resize", syncAccessibility);
    return () => window.removeEventListener("resize", syncAccessibility);
  }, [asideRef, dragging, pointerRef, visualOpen]);

  useEffect(() => {
    if (!visualOpen) return;
    const previousOverflow = document.body.style.overflow;
    const syncScrollLock = () => {
      document.body.style.overflow = window.innerWidth <= SIDEBAR_MOBILE_BREAKPOINT ? "hidden" : previousOverflow;
    };
    syncScrollLock();
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 120);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setVisualOpen(false);
        onCloseMobile();
        return;
      }
      if (event.key !== "Tab" || window.innerWidth > SIDEBAR_MOBILE_BREAKPOINT || !asideRef.current) return;
      const focusable = Array.from(asideRef.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
      ));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", syncScrollLock);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", syncScrollLock);
    };
  }, [asideRef, closeRef, onCloseMobile, setVisualOpen, visualOpen]);
}
