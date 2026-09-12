import type { MessageEditorContextPlacement } from "./messageEditorModel";

export interface MessageEditorRect {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
  height: number;
}

export function calculateMessageEditorContextPlacement(options: {
  workspaceRect: MessageEditorRect;
  anchorRect: MessageEditorRect;
  measuredHeight: number;
  mobile: boolean;
}): MessageEditorContextPlacement {
  const { workspaceRect, anchorRect, measuredHeight, mobile } = options;
  const gap = mobile ? 8 : 10;
  const edge = mobile ? 8 : 12;
  const availableWidth = Math.max(1, workspaceRect.width - edge * 2);
  const width = mobile
    ? Math.min(340, availableWidth)
    : Math.min(370, Math.max(280, availableWidth));

  if (mobile) {
    const anchorLeft = anchorRect.left - workspaceRect.left;
    const anchorTop = anchorRect.top - workspaceRect.top;
    const anchorBottom = anchorRect.bottom - workspaceRect.top;
    const left = Math.max(
      edge,
      Math.min(anchorLeft + anchorRect.width / 2 - width / 2, workspaceRect.width - width - edge),
    );
    const roomBelow = workspaceRect.height - anchorBottom - gap - edge;
    const roomAbove = anchorTop - gap - edge;
    let top: number;

    if (roomBelow >= measuredHeight) top = anchorBottom + gap;
    else if (roomAbove >= measuredHeight) top = anchorTop - measuredHeight - gap;
    else {
      top = Math.max(
        edge,
        Math.min(anchorTop - measuredHeight / 2, workspaceRect.height - measuredHeight - edge),
      );
    }

    return { left, top, width, side: "over" };
  }

  let side: MessageEditorContextPlacement["side"] = "right";
  let left = anchorRect.right - workspaceRect.left + gap;
  if (left + width > workspaceRect.width - edge) {
    side = "left";
    left = anchorRect.left - workspaceRect.left - width - gap;
  }
  if (left < edge) {
    side = "over";
    left = Math.max(edge, Math.min(anchorRect.left - workspaceRect.left, workspaceRect.width - width - edge));
  }

  const top = Math.max(
    edge,
    Math.min(anchorRect.top - workspaceRect.top, workspaceRect.height - measuredHeight - edge),
  );
  return { left, top, width, side };
}
