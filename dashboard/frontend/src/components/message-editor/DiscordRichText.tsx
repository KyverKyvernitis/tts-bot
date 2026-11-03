import { useState, type CSSProperties, type ReactNode } from "react";
import type { DashboardOptionsPayload } from "../../types/dashboard";
import { parseDiscordBlocks, parseDiscordInline, type DiscordInlineNode, type DiscordTextBlock, type DiscordTextFormat } from "./discordRichTextModel";

interface DiscordRichTextProps {
  text: string;
  guildOptions?: DashboardOptionsPayload | null;
  compact?: boolean;
  className?: string;
  /** Plain fields do not interpret message Markdown or Discord mentions. */
  format?: DiscordTextFormat;
}
function DiscordEmoji({ token }: { token: Extract<DiscordInlineNode, { type: "emoji" }> }) {
  const primaryExtension = token.animated ? "gif" : "webp";
  const fallbackExtension = token.animated ? "webp" : "png";
  const [extension, setExtension] = useState(primaryExtension);
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="osk-discord-emoji-fallback" title={token.raw}>:{token.name}:</span>;
  return <img className="osk-discord-emoji" src={`https://cdn.discordapp.com/emojis/${token.id}.${extension}?size=44&quality=lossless`}
    alt={`:${token.name}:`} title={`:${token.name}:`} loading="lazy" decoding="async" draggable={false}
    onError={() => { if (extension !== fallbackExtension) setExtension(fallbackExtension); else setFailed(true); }} />;
}
function mentionLabel(token: Extract<DiscordInlineNode, { type: "mention" | "variable" }>, guildOptions?: DashboardOptionsPayload | null): string {
  if (token.type === "mention") {
    if (token.kind === "role") return `@${guildOptions?.roles?.find(item => item.id === token.id)?.name ?? "cargo"}`;
    if (token.kind === "channel") return `#${guildOptions?.channels?.find(item => item.id === token.id)?.name ?? "canal"}`;
    return "@usuário";
  }
  if (token.mentionKind === "channel") return "#canal";
  const key = token.key.toLowerCase();
  return key.includes("convid") || key.includes("inviter") ? "@convidador" : "@membro";
}
function renderInline(nodes: DiscordInlineNode[], guildOptions: DashboardOptionsPayload | null | undefined, prefix: string): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${prefix}-${index}`;
    if (node.type === "text") return node.value;
    if (node.type === "code") return <code key={key}>{node.value}</code>;
    if (node.type === "emoji") return <DiscordEmoji key={`${key}-${node.raw}`} token={node} />;
    if (node.type === "link") return <span key={key} className="osk-discord-link" title={node.url}>{renderInline(node.children, guildOptions, key)}</span>;
    if (node.type === "mention" || node.type === "variable" && node.mentionKind) {
      return <span key={key} className="osk-discord-mention" data-kind={node.type === "mention" ? node.kind : node.mentionKind} title={node.raw}>{mentionLabel(node, guildOptions)}</span>;
    }
    if (node.type === "variable") return <span key={key} className="osk-discord-variable" title="Variável de template">{node.raw}</span>;
    const children = renderInline(node.children, guildOptions, key);
    if (node.style === "bold") return <strong key={key}>{children}</strong>;
    if (node.style === "italic") return <em key={key}>{children}</em>;
    if (node.style === "bold-italic") return <strong key={key}><em>{children}</em></strong>;
    if (node.style === "underline") return <u key={key}>{children}</u>;
    if (node.style === "strike") return <s key={key}>{children}</s>;
    return <span key={key} className="osk-discord-spoiler" title="Spoiler">{children}</span>;
  });
}
function renderBlock(block: DiscordTextBlock, index: number, guildOptions?: DashboardOptionsPayload | null): ReactNode {
  const key = `block-${index}`;
  if (block.type === "blank") return <span key={key} className="osk-discord-blank" aria-hidden="true">{"\n"}</span>;
  if (block.type === "code") return <span key={key} className="osk-discord-codeblock" data-language={block.language || undefined}><code>{block.text}</code></span>;
  if (block.type === "list") return <span key={key} className="osk-discord-list" role="list">{block.items.map((item, itemIndex) => <span className="osk-discord-list-item" role="listitem" key={itemIndex} style={{ "--osk-list-level": item.level } as CSSProperties}><span className="osk-discord-list-marker" aria-hidden="true">{item.marker}</span><span>{renderInline(parseDiscordInline(item.text), guildOptions, `${key}-${itemIndex}`)}</span></span>)}</span>;
  const children = renderInline(parseDiscordInline(block.text), guildOptions, key);
  if (block.type === "heading") return <span key={key} className="osk-discord-heading" data-level={block.level} role="heading" aria-level={block.level}>{children}</span>;
  if (block.type === "quote") return <span key={key} className="osk-discord-quote">{children}</span>;
  if (block.type === "subtext") return <span key={key} className="osk-discord-subtext">{children}</span>;
  return <span key={key} className="osk-discord-paragraph">{children}</span>;
}
export function DiscordRichText({ text, guildOptions, compact, className, format = compact ? "inline" : "full" }: DiscordRichTextProps) {
  if (!text.trim()) return null;
  const nodes = format === "plain" ? text : format === "inline"
    ? renderInline(parseDiscordInline(text), guildOptions, "inline")
    : parseDiscordBlocks(text).map((block, index) => renderBlock(block, index, guildOptions));
  return <span className={className ?? "osk-discord-rich-text"} data-compact={compact ? "true" : "false"} data-format={format}>{nodes}</span>;
}
