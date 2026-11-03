export type DiscordTextFormat = "plain" | "inline" | "full";
export type DiscordInlineNode =
  | { type: "text"; value: string }
  | { type: "code"; value: string }
  | { type: "style"; style: "bold" | "italic" | "bold-italic" | "underline" | "strike" | "spoiler"; children: DiscordInlineNode[] }
  | { type: "link"; url: string; children: DiscordInlineNode[] }
  | { type: "emoji"; raw: string; name: string; id: string; animated: boolean }
  | { type: "mention"; raw: string; kind: "user" | "role" | "channel"; id: string }
  | { type: "variable"; raw: string; key: string; mentionKind?: "user" | "channel" };
export type DiscordTextBlock =
  | { type: "text" | "quote" | "subtext"; text: string }
  | { type: "heading"; level: number; text: string }
  | { type: "code"; text: string; language: string }
  | { type: "list"; items: Array<{ text: string; marker: string; level: number }> }
  | { type: "blank" };

const TOKEN = /^(?:<a?:[^:\s>]+:\d{15,25}>|<@!?\d{15,25}>|<@&\d{15,25}>|<#\d{15,25}>|\$\{[A-Za-z0-9_]+\}|\{[A-Za-z0-9_]+\})/;
const ESCAPABLE = /[\\`*_{}[\]()#+\-.!>|~]/;
const STYLES: Array<[string, Extract<DiscordInlineNode, { type: "style" }>["style"]]> = [
  ["***", "bold-italic"], ["**", "bold"], ["__", "underline"], ["~~", "strike"], ["||", "spoiler"], ["*", "italic"], ["_", "italic"],
];
function escapedAt(text: string, index: number): boolean {
  let count = 0;
  while (index > 0 && text[--index] === "\\") count++;
  return count % 2 === 1;
}
function closingDelimiter(text: string, marker: string, from: number): number {
  for (let index = from; index < text.length; index++) {
    if (text[index] === "\\") { index++; continue; }
    // Code is opaque to the surrounding formatting markers.
    if (text[index] === "`") {
      const run = /^`+/.exec(text.slice(index))![0];
      const end = text.indexOf(run, index + run.length);
      if (end >= 0) { index = end + run.length - 1; continue; }
    }
    if (text[index] === marker[0]) {
      let runEnd = index + 1;
      while (text[runEnd] === marker[0]) runEnd++;
      // A bold run inside italics (or vice versa) is its own delimiter.
      if (runEnd - index !== marker.length) { index = runEnd - 1; continue; }
    }
    if (text.startsWith(marker, index) && index > from && !/\s/.test(text[index - 1])) {
      if (marker === "_" && /[\p{L}\p{N}]/u.test(text[index + 1] || "")) continue;
      return index;
    }
  }
  return -1;
}
function discordToken(raw: string): DiscordInlineNode {
  const emoji = /^<(a?):([^:\s>]+):(\d{15,25})>$/.exec(raw);
  if (emoji) return { type: "emoji", raw, animated: emoji[1] === "a", name: emoji[2], id: emoji[3] };
  if (raw.startsWith("<")) return { type: "mention", raw, kind: raw.startsWith("<#") ? "channel" : raw.startsWith("<@&") ? "role" : "user", id: raw.replace(/\D/g, "") };
  const key = raw.startsWith("${") ? raw.slice(2, -1) : raw.slice(1, -1);
  const lower = key.toLowerCase();
  const mentionKind = lower.includes("channel") || lower.includes("canal") ? "channel"
    : lower.includes("mention") || lower.includes("mencao") ? "user" : undefined;
  return { type: "variable", raw, key, mentionKind };
}
function maskedLink(text: string): { label: string; url: string; length: number } | null {
  if (text[0] !== "[") return null;
  let labelEnd = 1;
  for (; labelEnd < text.length; labelEnd++) if (text[labelEnd] === "]" && !escapedAt(text, labelEnd)) break;
  if (text[labelEnd + 1] !== "(") return null;
  let depth = 1;
  let end = labelEnd + 2;
  for (; end < text.length; end++) {
    if (escapedAt(text, end)) continue;
    if (text[end] === "(") depth++;
    if (text[end] === ")" && --depth === 0) break;
  }
  if (depth !== 0) return null;
  const url = text.slice(labelEnd + 2, end);
  if (!/^https?:\/\/[^\s<>]+$/i.test(url)) return null;
  return { label: text.slice(1, labelEnd), url, length: end + 1 };
}
/** Data-only parsing: original message values never change. */
export function parseDiscordInline(text: string, depth = 0): DiscordInlineNode[] {
  if (depth > 12) return [{ type: "text", value: text }];
  const nodes: DiscordInlineNode[] = [];
  let buffer = "";
  const flush = () => { if (buffer) { nodes.push({ type: "text", value: buffer }); buffer = ""; } };
  for (let index = 0; index < text.length;) {
    const character = text[index];
    if (character === "\\" && ESCAPABLE.test(text[index + 1] || "")) { buffer += text[index + 1]; index += 2; continue; }
    if (character === "`") {
      const marker = /^`+/.exec(text.slice(index))![0];
      const end = text.indexOf(marker, index + marker.length);
      if (end > index + marker.length) {
        flush(); nodes.push({ type: "code", value: text.slice(index + marker.length, end) }); index = end + marker.length; continue;
      }
    }
    if (character === "<" || character === "{" || character === "$") {
      const raw = TOKEN.exec(text.slice(index))?.[0];
      if (raw) { flush(); nodes.push(discordToken(raw)); index += raw.length; continue; }
    }
    if (character === "[") {
      const link = maskedLink(text.slice(index));
      if (link) {
        flush(); nodes.push({ type: "link", url: link.url, children: parseDiscordInline(link.label, depth + 1) }); index += link.length; continue;
      }
    }
    const urlMatch = /^https?:\/\/[^\s<>]+/i.exec(text.slice(index));
    if (urlMatch) {
      let url = urlMatch[0].replace(/[.,!?;:*_~|]+$/, "");
      while (url.endsWith(")") && (url.match(/\)/g)?.length || 0) > (url.match(/\(/g)?.length || 0)) url = url.slice(0, -1);
      if (url) { flush(); nodes.push({ type: "link", url, children: [{ type: "text", value: url }] }); index += url.length; continue; }
    }
    let formatted = false;
    for (const [marker, style] of STYLES) {
      if (!text.startsWith(marker, index) || /\s/.test(text[index + marker.length] || " ")) continue;
      if (marker === "_" && /[\p{L}\p{N}]/u.test(text[index - 1] || "")) continue;
      const end = closingDelimiter(text, marker, index + marker.length);
      if (end < 0) continue;
      flush(); nodes.push({ type: "style", style, children: parseDiscordInline(text.slice(index + marker.length, end), depth + 1) });
      index = end + marker.length; formatted = true; break;
    }
    if (!formatted) { buffer += character; index++; }
  }
  flush(); return nodes;
}
const LIST_ITEM = /^([ \t]*)([-+*]|\d+[.)])\s+(.+)$/;
const FENCE = /^\s*(`{3,})([A-Za-z0-9_+-]*)\s*$/;
function structuralLine(line: string): boolean {
  return !line.trim() || FENCE.test(line) || /^ {0,3}(?:#{1,3}\s+|-#\s+|>\s?)/.test(line) || LIST_ITEM.test(line);
}
export function parseDiscordBlocks(text: string): DiscordTextBlock[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: DiscordTextBlock[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { blocks.push({ type: "blank" }); index++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      const body: string[] = []; index++;
      while (index < lines.length && !new RegExp(`^\\s*${fence[1]}\\s*$`).test(lines[index])) body.push(lines[index++]);
      if (index < lines.length) index++;
      blocks.push({ type: "code", language: fence[2], text: body.join("\n") }); continue;
    }
    const heading = /^ {0,3}(#{1,3})\s+(.+)$/.exec(line);
    if (heading) { blocks.push({ type: "heading", level: heading[1].length, text: heading[2] }); index++; continue; }
    const subtext = /^ {0,3}-#\s+(.*)$/.exec(line);
    if (subtext) { blocks.push({ type: "subtext", text: subtext[1] }); index++; continue; }
    const multilineQuote = /^ {0,3}>>>\s?(.*)$/.exec(line);
    if (multilineQuote) { blocks.push({ type: "quote", text: [multilineQuote[1], ...lines.slice(index + 1)].join("\n") }); break; }
    if (/^ {0,3}>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^ {0,3}>\s?/.test(lines[index])) body.push(lines[index++].replace(/^ {0,3}>\s?/, ""));
      blocks.push({ type: "quote", text: body.join("\n") }); continue;
    }
    if (LIST_ITEM.test(line)) {
      const items: Extract<DiscordTextBlock, { type: "list" }>["items"] = [];
      let item: RegExpExecArray | null;
      while (index < lines.length && (item = LIST_ITEM.exec(lines[index]))) {
        items.push({ marker: /\d/.test(item[2][0]) ? item[2] : "•", level: Math.min(6, Math.floor(item[1].replace(/\t/g, "  ").length / 2)), text: item[3] }); index++;
      }
      blocks.push({ type: "list", items }); continue;
    }
    const body = [line];
    while (++index < lines.length && !structuralLine(lines[index])) body.push(lines[index]);
    blocks.push({ type: "text", text: body.join("\n") });
  }
  return blocks;
}
