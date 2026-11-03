import { useEffect, useState } from "react";

export interface DashboardViewportState {
  keyboardOpen: boolean;
  /** Fixed-element offset only; browsers that resize the layout return zero. */
  dockBottom: number;
  viewportHeight: number;
}

export interface DashboardViewportMetrics {
  baselineHeight: number;
  layoutHeight: number;
  visualHeight: number;
  offsetTop: number;
  scale: number;
  mobile: boolean;
  focusedEditable: boolean;
  wasKeyboardOpen: boolean;
}

/** Keep zoom and browser chrome changes distinct from a software keyboard. */
export function dashboardViewportState(metrics: DashboardViewportMetrics): DashboardViewportState {
  const height = Math.max(0, metrics.visualHeight);
  const shrink = Math.max(0, metrics.baselineHeight - height);
  const keyboardOpen = metrics.mobile && metrics.scale <= 1.1 && (
    (metrics.focusedEditable && shrink >= 150)
    || (metrics.wasKeyboardOpen && shrink >= 90)
  );
  return {
    keyboardOpen,
    dockBottom: keyboardOpen
      ? Math.max(0, Math.round(metrics.layoutHeight - height - metrics.offsetTop))
      : 0,
    viewportHeight: Math.round(height),
  };
}

function isEditable(element: Element | null): boolean {
  if (!(element instanceof HTMLElement)) return false;
  if (element.isContentEditable) return true;
  if (element instanceof HTMLTextAreaElement) return !element.disabled && !element.readOnly;
  if (element instanceof HTMLInputElement) {
    return !element.disabled && !element.readOnly
      && !["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"].includes(element.type);
  }
  return false;
}

/** Measures the visible workspace without retaining listeners between mounts. */
export function useDashboardViewport(): DashboardViewportState {
  const [state, setState] = useState<DashboardViewportState>({ keyboardOpen: false, dockBottom: 0, viewportHeight: 0 });

  useEffect(() => {
    const viewport = window.visualViewport;
    let baselineHeight = Math.max(window.innerHeight, viewport?.height || 0);
    let previousWidth = window.innerWidth;
    let keyboardOpen = false;
    let frame = 0;
    let settleTimer: number | null = null;

    function measure() {
      frame = 0;
      const layoutHeight = window.innerHeight;
      const visualHeight = viewport?.height || layoutHeight;
      const focusedEditable = isEditable(document.activeElement);
      const mobile = window.innerWidth <= 980;
      const widthChanged = Math.abs(previousWidth - window.innerWidth) > 100;

      // Capture the normal viewport before typing. Do not lower the baseline on
      // blur while the keyboard is animating away, or the dock would jump down.
      if (mobile && widthChanged && keyboardOpen) {
        // On rotation, the previous width is a useful unoccluded height even
        // when this browser has already resized innerHeight for the keyboard.
        baselineHeight = Math.max(layoutHeight, visualHeight, previousWidth);
      } else if (!mobile || (!focusedEditable && !keyboardOpen) || (widthChanged && !keyboardOpen)) {
        baselineHeight = Math.max(layoutHeight, visualHeight);
      } else if (!keyboardOpen) {
        baselineHeight = Math.max(baselineHeight, layoutHeight, visualHeight);
      }
      previousWidth = window.innerWidth;
      const next = dashboardViewportState({
        baselineHeight,
        layoutHeight,
        visualHeight,
        offsetTop: viewport?.offsetTop || 0,
        scale: viewport?.scale || 1,
        mobile,
        focusedEditable,
        wasKeyboardOpen: keyboardOpen,
      });
      keyboardOpen = next.keyboardOpen;
      setState(current => current.keyboardOpen === next.keyboardOpen
        && current.dockBottom === next.dockBottom
        && current.viewportHeight === next.viewportHeight ? current : next);
    }

    function schedule() {
      if (!frame) frame = window.requestAnimationFrame(measure);
    }

    function focusChanged() {
      schedule();
      if (settleTimer !== null) window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(schedule, 250);
    }

    measure();
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    document.addEventListener("focusin", focusChanged);
    document.addEventListener("focusout", focusChanged);
    return () => {
      window.cancelAnimationFrame(frame);
      if (settleTimer !== null) window.clearTimeout(settleTimer);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      document.removeEventListener("focusin", focusChanged);
      document.removeEventListener("focusout", focusChanged);
    };
  }, []);

  return state;
}
