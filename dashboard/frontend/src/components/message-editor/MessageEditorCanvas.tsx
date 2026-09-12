import { Braces } from "lucide-react";
import type { RefObject } from "react";
import { MessagePreview } from "./MessagePreview";
import type { MessagePreviewProps } from "./messagePreviewTypes";

export interface MessageEditorCanvasProps {
  canvasPaneRef: RefObject<HTMLElement>;
  interactive: boolean;
  hidden: boolean;
  preview: MessagePreviewProps;
}

export function MessageEditorCanvas({ canvasPaneRef, interactive, hidden, preview }: MessageEditorCanvasProps) {
  return (
    <section ref={canvasPaneRef} className="osk-message-editor__canvas-pane" aria-hidden={hidden || undefined}>
      <MessagePreview {...preview} />
      {!interactive && <div className="osk-message-editor__canvas-lock"><Braces size={17} /><span>Aplique ou descarte o JSON pendente para voltar à edição visual.</span></div>}
    </section>
  );
}
