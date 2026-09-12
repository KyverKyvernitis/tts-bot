import assert from "node:assert/strict";
import test from "node:test";
import { calculateMessageEditorContextPlacement, type MessageEditorRect } from "../src/components/message-editor/messageEditorLayout";

const rect = (left: number, top: number, width: number, height: number): MessageEditorRect => ({
  left,
  top,
  width,
  height,
  right: left + width,
  bottom: top + height,
});

test("desktop posiciona painel à direita quando há espaço", () => {
  assert.deepEqual(calculateMessageEditorContextPlacement({
    workspaceRect: rect(100, 50, 1000, 700),
    anchorRect: rect(250, 150, 100, 40),
    measuredHeight: 300,
    mobile: false,
  }), { left: 260, top: 100, width: 370, side: "right" });
});

test("desktop troca para esquerda perto da borda direita", () => {
  const result = calculateMessageEditorContextPlacement({
    workspaceRect: rect(100, 50, 800, 600),
    anchorRect: rect(750, 150, 100, 40),
    measuredHeight: 240,
    mobile: false,
  });
  assert.equal(result.side, "left");
  assert.equal(result.left, 270);
  assert.equal(result.top, 100);
});

test("desktop sobrepõe quando nenhum lado comporta a largura mínima", () => {
  const result = calculateMessageEditorContextPlacement({
    workspaceRect: rect(0, 0, 300, 500),
    anchorRect: rect(120, 80, 60, 40),
    measuredHeight: 200,
    mobile: false,
  });
  assert.equal(result.side, "over");
  assert.equal(result.left, 12);
  assert.equal(result.width, 280);
});

test("mobile prioriza abaixo e centraliza no anchor", () => {
  assert.deepEqual(calculateMessageEditorContextPlacement({
    workspaceRect: rect(0, 0, 400, 700),
    anchorRect: rect(120, 100, 80, 40),
    measuredHeight: 200,
    mobile: true,
  }), { left: 8, top: 148, width: 340, side: "over" });
});

test("mobile usa espaço acima quando não cabe abaixo", () => {
  const result = calculateMessageEditorContextPlacement({
    workspaceRect: rect(0, 0, 400, 500),
    anchorRect: rect(150, 420, 60, 40),
    measuredHeight: 180,
    mobile: true,
  });
  assert.equal(result.side, "over");
  assert.equal(result.top, 232);
});

test("posição vertical é limitada às bordas do workspace", () => {
  const result = calculateMessageEditorContextPlacement({
    workspaceRect: rect(10, 20, 900, 300),
    anchorRect: rect(300, 280, 80, 30),
    measuredHeight: 260,
    mobile: false,
  });
  assert.equal(result.top, 28);
});
