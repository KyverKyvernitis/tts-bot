import { useCallback, useEffect, useState } from "react";
import { calculateMessageEditorContextPlacement } from "./messageEditorLayout";
import type { MessageEditorContextPlacement, MessageEditorView } from "./messageEditorModel";

interface MutableRefLike<T> {
  current: T | null;
}

interface UseMessageEditorContextPlacementOptions {
  view: MessageEditorView;
  contextAnchorFieldId: string | null;
  draft: Record<string, unknown>;
  workspaceRef: MutableRefLike<HTMLDivElement>;
  canvasPaneRef: MutableRefLike<HTMLElement>;
  contextPanelRef: MutableRefLike<HTMLElement>;
}

export function useMessageEditorContextPlacement({
  view,
  contextAnchorFieldId,
  draft,
  workspaceRef,
  canvasPaneRef,
  contextPanelRef,
}: UseMessageEditorContextPlacementOptions) {
  const [contextPlacement, setContextPlacement] = useState<MessageEditorContextPlacement | null>(null);

  const updateContextPlacement = useCallback(() => {
    if (view !== "inspector" || !contextAnchorFieldId || !workspaceRef.current) {
      setContextPlacement(null);
      return;
    }

    const workspace = workspaceRef.current;
    const anchor = Array.from(workspace.querySelectorAll<HTMLElement>("[data-message-field-anchor]"))
      .find((element) => element.dataset.messageFieldAnchor === contextAnchorFieldId);
    if (!anchor) {
      setContextPlacement(null);
      return;
    }

    const workspaceRect = workspace.getBoundingClientRect();
    const anchorRect = anchor.getBoundingClientRect();
    const mobile = window.matchMedia("(max-width: 899px)").matches;
    const edge = mobile ? 8 : 12;
    const measuredHeight = Math.min(
      contextPanelRef.current?.getBoundingClientRect().height || (mobile ? 150 : 300),
      Math.max(120, workspaceRect.height - edge * 2),
    );
    setContextPlacement(calculateMessageEditorContextPlacement({
      workspaceRect,
      anchorRect,
      measuredHeight,
      mobile,
    }));
  }, [contextAnchorFieldId, contextPanelRef, view, workspaceRef]);

  useEffect(() => {
    if (view !== "inspector") {
      setContextPlacement(null);
      return;
    }
    const firstFrame = window.requestAnimationFrame(() => {
      updateContextPlacement();
      window.requestAnimationFrame(updateContextPlacement);
    });
    const canvas = canvasPaneRef.current;
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateContextPlacement);
    if (workspaceRef.current) observer?.observe(workspaceRef.current);
    if (contextPanelRef.current) observer?.observe(contextPanelRef.current);
    canvas?.addEventListener("scroll", updateContextPlacement, { passive: true });
    window.addEventListener("resize", updateContextPlacement);
    return () => {
      window.cancelAnimationFrame(firstFrame);
      observer?.disconnect();
      canvas?.removeEventListener("scroll", updateContextPlacement);
      window.removeEventListener("resize", updateContextPlacement);
    };
  }, [canvasPaneRef, contextAnchorFieldId, contextPanelRef, draft, updateContextPlacement, view, workspaceRef]);

  return { contextPlacement, setContextPlacement };
}
