import { Bold, Check, Code2, EyeOff, Italic, Link2, Quote, Strikethrough, Underline, Variable } from "lucide-react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { DashboardFieldDefinition } from "../../types/dashboard";

export interface MessageEditorTextDockProps {
  field: DashboardFieldDefinition;
  value: string;
  notice: string | null;
  hasVariables: boolean;
  onWrap(prefix: string, suffix: string, placeholder: string): void;
  onPrefixLines(prefix: string, placeholder: string): void;
  onVariables(): void;
  onDone(): void;
}

export function MessageEditorTextDock({ field, value, notice, hasVariables, onWrap, onPrefixLines, onVariables, onDone }: MessageEditorTextDockProps) {
  const preventToolbarBlur = (event: ReactPointerEvent<HTMLButtonElement>) => event.preventDefault();
  return (
    <div className="osk-message-editor__text-dock">
      <div className="osk-message-editor__text-dock-copy">
        <strong>{field.label}</strong>
        {field.maxLength ? <span>{value.length}/{field.maxLength}</span> : null}
        {notice && <em role="status">{notice}</em>}
      </div>
      <div className="osk-message-editor__text-dock-tools" aria-label="Formatação de texto">
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("**", "**", "texto")} aria-label="Negrito" title="Negrito"><Bold size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("*", "*", "texto")} aria-label="Itálico" title="Itálico"><Italic size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("__", "__", "texto")} aria-label="Sublinhado" title="Sublinhado"><Underline size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("~~", "~~", "texto")} aria-label="Tachado" title="Tachado"><Strikethrough size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("||", "||", "texto")} aria-label="Spoiler" title="Spoiler"><EyeOff size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onPrefixLines("> ", "texto")} aria-label="Citação" title="Citação"><Quote size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("`", "`", "código")} aria-label="Código" title="Código"><Code2 size={16} /></button>
        <button type="button" onPointerDown={preventToolbarBlur} onClick={() => onWrap("[", "](https://)", "texto do link")} aria-label="Link" title="Link"><Link2 size={16} /></button>
        {hasVariables ? <button type="button" onPointerDown={preventToolbarBlur} onClick={onVariables} aria-label="Inserir variável" title="Inserir variável"><Variable size={16} /></button> : null}
      </div>
      <button type="button" className="osk-message-editor__text-done" onPointerDown={preventToolbarBlur} onClick={onDone}><Check size={16} />Pronto</button>
    </div>
  );
}
