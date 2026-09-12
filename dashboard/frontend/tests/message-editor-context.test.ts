import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DiscordRichText } from "../src/components/message-editor/DiscordRichText";
import { MessageVisualEditor } from "../src/components/message-editor/MessageVisualEditor";
import {
  discordAttachmentPreviewProxyUrl,
  discordAttachmentUrlInfo,
  normalizePreviewUrl,
  previewImageCandidates,
} from "../src/components/message-editor/messageEditorUtils";
import type { DashboardFieldDefinition } from "../src/types/dashboard";

const imageModeField: DashboardFieldDefinition = {
  id: "welcome.embed.thumbnail_mode",
  label: "Thumbnail",
  type: "select",
  scope: "welcome",
  path: "embed.thumbnail_mode",
  options: [
    { value: "none", label: "Sem thumbnail" },
    { value: "member", label: "Avatar do membro" },
    { value: "server", label: "Avatar do servidor" },
    { value: "custom", label: "Imagem personalizada" },
  ],
};

function renderEditor(contextual: boolean) {
  return renderToStaticMarkup(React.createElement(MessageVisualEditor, {
    fields: [imageModeField],
    baseline: { [imageModeField.id]: "none" },
    draft: { [imageModeField.id]: "member" },
    guildOptions: null,
    contextual,
    onChange() {},
  }));
}

test("mantém escolhas curtas fechadas até o usuário abrir o dropdown", () => {
  const html = renderEditor(true);

  assert.match(html, /class="osk-message-context-select__trigger"/);
  assert.match(html, /Avatar do membro/);
  assert.match(html, /aria-expanded="false"/);
  assert.doesNotMatch(html, /role="listbox"/);
  assert.doesNotMatch(html, /osk-message-context-options/);
  assert.doesNotMatch(html, /class="osk-select-trigger"/);
});

test("mantém o seletor reutilizável fora do editor contextual", () => {
  const html = renderEditor(false);

  assert.match(html, /class="osk-select-trigger"/);
  assert.doesNotMatch(html, /class="osk-message-context-options"/);
});

test("mantém o canvas montado enquanto propriedades, variáveis ou JSON estão abertos", () => {
  const editor = readFileSync(new URL("../src/components/message-editor/MessageEditor.tsx", import.meta.url), "utf8");
  const surface = readFileSync(new URL("../src/components/message-editor/MessageEditorSurface.tsx", import.meta.url), "utf8");
  const canvas = readFileSync(new URL("../src/components/message-editor/MessageEditorCanvas.tsx", import.meta.url), "utf8");
  const context = readFileSync(new URL("../src/components/message-editor/MessageEditorContextPanel.tsx", import.meta.url), "utf8");

  assert.match(editor, /<MessageEditorSurface/);
  assert.match(surface, /<MessageEditorCanvas/);
  assert.match(surface, /context \? <MessageEditorContextPanel/);
  assert.match(surface, /data-context-open=\{view !== "canvas" \|\| undefined\}/);
  assert.match(canvas, /className="osk-message-editor__canvas-pane"/);
  assert.match(context, /className="osk-message-editor__context-layer"/);
  assert.ok(surface.indexOf("<MessageEditorCanvas") < surface.indexOf("<MessageEditorContextPanel"));
});

test("mantém refs DOM compatíveis com os elementos JSX", () => {
  const editor = readFileSync(new URL("../src/components/message-editor/MessageEditor.tsx", import.meta.url), "utf8");
  const surface = readFileSync(new URL("../src/components/message-editor/MessageEditorSurface.tsx", import.meta.url), "utf8");
  const canvas = readFileSync(new URL("../src/components/message-editor/MessageEditorCanvas.tsx", import.meta.url), "utf8");
  const context = readFileSync(new URL("../src/components/message-editor/MessageEditorContextPanel.tsx", import.meta.url), "utf8");

  assert.match(editor, /useRef<HTMLElement>\(null\)/);
  assert.match(editor, /useRef<HTMLDivElement>\(null\)/);
  assert.match(canvas, /canvasPaneRef: RefObject<HTMLElement>;/);
  assert.match(context, /panelRef: RefObject<HTMLElement>;/);
  assert.match(surface, /dialogRef: RefObject<HTMLDivElement>;/);
  assert.doesNotMatch(`${editor}\n${surface}\n${canvas}\n${context}`, /RefObject<(?:HTML\w*Element|HTMLElement) \| null>/);
});

test("mantém salvar e descartar visíveis junto da barra de formatação", () => {
  const editor = readFileSync(new URL("../src/components/message-editor/MessageEditor.tsx", import.meta.url), "utf8");
  const surface = readFileSync(new URL("../src/components/message-editor/MessageEditorSurface.tsx", import.meta.url), "utf8");
  const dock = readFileSync(new URL("../src/components/message-editor/MessageEditorTextDock.tsx", import.meta.url), "utf8");

  assert.match(editor, /<MessageEditorSurface/);
  assert.match(surface, /textDock \? <MessageEditorTextDock/);
  assert.match(surface, /<footer className="osk-message-editor__footer"/);
  assert.match(dock, /className="osk-message-editor__text-dock"/);
});

test("limita o editor à viewport pequena sem herdar a altura da página", () => {
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(css, /\.osk-message-editor\s*\{[^}]*min-height:\s*0\s*!important/s);
  assert.match(css, /height:\s*min\(var\(--osk-message-editor-viewport-height,\s*100dvh\),\s*100svh\)\s*!important/);
  assert.match(css, /max-height:\s*min\(var\(--osk-message-editor-viewport-height,\s*100dvh\),\s*100svh\)/);
});

test("mantém as ações do JSON dentro do painel compacto", () => {
  const source = readFileSync(new URL("../src/components/message-editor/MessageJsonEditor.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/design-refresh.css", import.meta.url), "utf8");

  assert.doesNotMatch(source, /<strong>JSON avançado<\/strong>/);
  assert.match(source, /className="osk-message-json__actions"/);
  assert.match(css, /data-view="json"\][^{]*\.osk-message-json__textarea\s*\{[^}]*min-height:\s*160px[^}]*max-height:\s*360px/s);
  assert.match(css, /data-view="json"\][^{]*\.osk-message-editor__context-popover\s*\{[^}]*max-height:\s*min\(62svh,\s*540px,\s*calc\(100% - 16px\)\)/s);
});

test("normaliza e cria fallback autenticado para anexos do Discord", () => {
  const url = "https:\\/\\/cdn.discordapp.com\\/attachments\\/123456789012345\\/987654321098765\\/image.png?ex=ffffffff\\u0026is=eeeeeeee\\u0026hm=abc";
  const normalized = normalizePreviewUrl(`\u200B<${url}>`);
  const candidates = previewImageCandidates(normalized);

  assert.equal(normalized, "https://cdn.discordapp.com/attachments/123456789012345/987654321098765/image.png?ex=ffffffff&is=eeeeeeee&hm=abc");
  assert.equal(candidates[0], normalized);
  assert.match(candidates[1], /^https:\/\/media\.discordapp\.net\/attachments\//);
  assert.equal(candidates[2], discordAttachmentPreviewProxyUrl(normalized));
  assert.match(candidates[2], /^\/api\/dashboard\/media-preview\?url=/);
});

test("identifica expiração de links assinados do Discord", () => {
  const expired = "https://cdn.discordapp.com/attachments/123456789012345/987654321098765/image.png?ex=00000010&is=0&hm=abc";
  const active = expired.replace("ex=00000010", "ex=ffffffff");

  assert.deepEqual(discordAttachmentUrlInfo(expired, 20_000), { expiresAt: 16_000, expired: true });
  assert.equal(discordAttachmentUrlInfo(active, 20_000)?.expired, false);
  assert.equal(discordAttachmentUrlInfo("https://example.com/image.png", 20_000), null);
});

test("ancora o inspetor ao elemento tocado também no mobile", () => {
  const placement = readFileSync(new URL("../src/components/message-editor/useMessageEditorContextPlacement.ts", import.meta.url), "utf8");
  const layout = readFileSync(new URL("../src/components/message-editor/messageEditorLayout.ts", import.meta.url), "utf8");
  const context = readFileSync(new URL("../src/components/message-editor/MessageEditorContextPanel.tsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/design-refresh.css", import.meta.url), "utf8");

  assert.match(placement, /const mobile = window\.matchMedia\("\(max-width: 899px\)"\)\.matches/);
  assert.match(placement, /calculateMessageEditorContextPlacement/);
  assert.match(layout, /return \{ left, top, width, side: "over" \}/);
  assert.match(context, /data-anchored=\{view === "inspector" && anchorFieldId \? true : undefined\}/);
  assert.match(
    css,
    /data-view="inspector"\]\[data-anchored\][^{]*\{[^}]*top:\s*var\(--osk-message-context-top[^}]*bottom:\s*auto[^}]*left:\s*var\(--osk-message-context-left/s,
  );
});

test("mantém a prévia legível e limita o painel contextual no mobile", () => {
  const css = readFileSync(new URL("../src/design-refresh.css", import.meta.url), "utf8");

  assert.match(css, /\.osk-message-editor__context-backdrop\s*\{[^}]*backdrop-filter:\s*none/s);
  assert.match(css, /max-height:\s*min\(50dvh,\s*430px\)/);
  assert.match(css, /\.osk-message-context-select__menu\s*\{[^}]*position:\s*fixed[^}]*max-height:\s*var\(--osk-context-select-max-height/s);
});

test("o mesmo editor compartilhado continua atendendo todas as mensagens dos módulos", () => {
  const sectionEditor = readFileSync(new URL("../src/components/SectionEditor.tsx", import.meta.url), "utf8");
  const panels = readFileSync(new URL("../src/components/SectionEditorPanels.tsx", import.meta.url), "utf8");

  assert.match(sectionEditor, /import \{ MessageEditor \} from "\.\/message-editor"/);
  assert.match(sectionEditor, /<MessageGroupPanel/);
  assert.match(sectionEditor, /<MessageEditor/);
  assert.match(panels, /metadata\.editors\?\.length/);
  assert.match(panels, /createLegacyMessageEditor\(group, fields\)/);
});

test("a prévia interpreta as opções expostas pela barra de formatação", () => {
  const html = renderToStaticMarkup(React.createElement(DiscordRichText, {
    text: "__sublinhado__ ||segredo||\n> citação\n[documentação](https://example.com)",
  }));

  assert.match(html, /<u>sublinhado<\/u>/);
  assert.match(html, /class="osk-discord-spoiler"/);
  assert.match(html, /class="osk-discord-quote"/);
  assert.match(html, /class="osk-discord-link" title="https:\/\/example\.com">documentação/);
});
