import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { BUNDLED_DECORATIVE_IMAGE_URL, BUNDLED_LOADING_GIF_URL, resolveCompletedImageState } from "../src/app/visualAssets";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(projectRoot, "src/components/VisualTemplates.tsx"), "utf8");
const theme = readFileSync(join(projectRoot, "src/yin-yang-theme.css"), "utf8");

test("usa os assets originais de public/assets por padrão", () => {
  assert.equal(BUNDLED_DECORATIVE_IMAGE_URL, "/assets/osaka-landing-character.jpg");
  assert.equal(BUNDLED_LOADING_GIF_URL, "/assets/osaka-loading.gif");
  assert.match(theme, /url\(["']?\/assets\/osaka-landing-stars\.jpg["']?\)/);
});

test("mantém fallback de carregamento quando a imagem falha", () => {
  assert.match(source, /state !== "ready" \? <LoaderCircle/);
  assert.match(source, /if \(!decorativeImageUrl \|\| state === "failed"\) return null/);
});

test("reconhece imediatamente imagens restauradas do cache", () => {
  assert.equal(resolveCompletedImageState(null), null);
  assert.equal(resolveCompletedImageState({ complete: false, naturalWidth: 0 }), null);
  assert.equal(resolveCompletedImageState({ complete: true, naturalWidth: 555 }), "ready");
  assert.equal(resolveCompletedImageState({ complete: true, naturalWidth: 0 }), "failed");
});

test("mantém as imagens opcionais visíveis quando carregadas", () => {
  assert.match(theme, /\.osk-decorative-template\s*\{[\s\S]*?opacity:\s*var\(--osk-decorative-opacity\)/);
  assert.match(theme, /\.osk-loading-visual img\s*\{[\s\S]*?opacity:\s*1/);
});

test("mantém estrelas em CSS como fallback do fundo original", () => {
  assert.match(theme, /\.osk-minimal-landing\s*\{[\s\S]*?min-height:\s*100dvh/);
  assert.match(theme, /\.osk-minimal-landing::before\s*\{[\s\S]*?linear-gradient[\s\S]*?osaka-landing-stars\.jpg[\s\S]*?radial-gradient/s);
  assert.match(theme, /background-size:\s*cover,\s*cover,\s*88px 88px,\s*112px 112px,\s*136px 136px/);
});
