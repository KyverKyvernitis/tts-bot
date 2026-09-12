import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { accountMenuPosition, adjacentAccountMenuIndex } from "./accountMenuModel";

const CLOSE_MS = 180;
const FOCUSABLE_SELECTOR = "a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])";

export function useAccountMenu() {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sheetRef = useRef<HTMLElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const openFrameOneRef = useRef<number | null>(null);
  const openFrameTwoRef = useRef<number | null>(null);
  const menuId = useId();
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, right: 0, maxHeight: 320 });

  const measure = useCallback(() => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition(accountMenuPosition(
      rect,
      window.innerWidth,
      window.visualViewport?.height ?? window.innerHeight,
    ));
  }, []);

  const clearOpenFrames = useCallback(() => {
    if (openFrameOneRef.current !== null) window.cancelAnimationFrame(openFrameOneRef.current);
    if (openFrameTwoRef.current !== null) window.cancelAnimationFrame(openFrameTwoRef.current);
    openFrameOneRef.current = null;
    openFrameTwoRef.current = null;
  }, []);

  const open = useCallback(() => {
    clearOpenFrames();
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    measure();
    setMounted(true);
    openFrameOneRef.current = window.requestAnimationFrame(() => {
      openFrameTwoRef.current = window.requestAnimationFrame(() => {
        openFrameOneRef.current = null;
        openFrameTwoRef.current = null;
        setVisible(true);
      });
    });
  }, [clearOpenFrames, measure]);

  const close = useCallback((restoreFocus = true) => {
    clearOpenFrames();
    setVisible(false);
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = window.setTimeout(() => {
      setMounted(false);
      closeTimerRef.current = null;
    }, CLOSE_MS + 40);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
  }, [clearOpenFrames]);

  const toggle = useCallback(() => {
    if (mounted && visible) close();
    else open();
  }, [close, mounted, open, visible]);

  useLayoutEffect(() => {
    if (mounted) measure();
  }, [measure, mounted]);

  useEffect(() => {
    if (!mounted) return;
    const focusTimer = visible
      ? window.setTimeout(() => sheetRef.current?.querySelector<HTMLElement>("[role='menuitem']:not([disabled])")?.focus(), 60)
      : null;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      const items = Array.from(sheetRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([disabled])") ?? []);
      if (!items.length) return;
      const current = items.indexOf(document.activeElement as HTMLElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        items[adjacentAccountMenuIndex(current, items.length, direction)]?.focus();
      } else if (event.key === "Home") {
        event.preventDefault();
        items[0]?.focus();
      } else if (event.key === "End") {
        event.preventDefault();
        items[items.length - 1]?.focus();
      } else if (event.key === "Tab") {
        event.preventDefault();
        const backwards = event.shiftKey;
        close(false);
        window.requestAnimationFrame(() => {
          const trigger = triggerRef.current;
          if (!trigger) return;
          if (backwards) {
            trigger.focus({ preventScroll: true });
            return;
          }
          const focusable = Array.from(document.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
            .filter((element) => !element.closest(".osk-account-layer") && (element === trigger || element.getClientRects().length > 0));
          const triggerIndex = focusable.indexOf(trigger);
          (focusable[triggerIndex + 1] || trigger).focus({ preventScroll: true });
        });
      }
    };
    const onViewportChange = () => measure();
    const visualViewport = window.visualViewport;
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    visualViewport?.addEventListener("resize", onViewportChange);
    visualViewport?.addEventListener("scroll", onViewportChange);
    return () => {
      if (focusTimer !== null) window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
      visualViewport?.removeEventListener("resize", onViewportChange);
      visualViewport?.removeEventListener("scroll", onViewportChange);
    };
  }, [close, measure, mounted, visible]);

  useEffect(() => () => {
    clearOpenFrames();
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, [clearOpenFrames]);

  const run = useCallback((action: () => void, restoreFocus = true) => {
    close(restoreFocus);
    action();
  }, [close]);

  return { triggerRef, sheetRef, menuId, mounted, visible, position, open, close, toggle, run };
}
