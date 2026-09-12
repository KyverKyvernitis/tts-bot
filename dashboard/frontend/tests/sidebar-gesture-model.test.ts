import assert from "node:assert/strict";
import test from "node:test";
import {
  SIDEBAR_EDGE_GESTURE_MIN_X,
  sidebarDragOffset,
  sidebarDrawerProgress,
  sidebarGestureAxis,
  sidebarGestureIsHorizontal,
  sidebarMaxGestureStartX,
  sidebarShouldClose,
  sidebarShouldOpen,
} from "../src/components/sidebar/sidebarGestureModel";

test("limita início do gesto à borda útil do viewport", () => {
  assert.equal(sidebarMaxGestureStartX(300), 96);
  assert.equal(sidebarMaxGestureStartX(1000), 144);
  assert.equal(SIDEBAR_EDGE_GESTURE_MIN_X, 16);
});

test("trava eixo horizontal somente na direção esperada", () => {
  assert.equal(sidebarGestureAxis("opening", 20, 2), "horizontal");
  assert.equal(sidebarGestureAxis("opening", -20, 2), "pending");
  assert.equal(sidebarGestureAxis("closing", -20, 2), "horizontal");
  assert.equal(sidebarGestureAxis("closing", 20, 2), "pending");
  assert.equal(sidebarGestureAxis("opening", 3, 20), "vertical");
});

test("clampa deslocamento dentro da largura do drawer", () => {
  assert.equal(sidebarDragOffset("opening", -50, 280), -280);
  assert.equal(sidebarDragOffset("opening", 100, 280), -180);
  assert.equal(sidebarDragOffset("opening", 400, 280), 0);
  assert.equal(sidebarDragOffset("closing", 30, 280), 0);
  assert.equal(sidebarDragOffset("closing", -100, 280), -100);
  assert.equal(sidebarDragOffset("closing", -400, 280), -280);
});

test("decide abertura e fechamento por distância ou velocidade", () => {
  assert.equal(sidebarShouldOpen(64, 0, 280), true);
  assert.equal(sidebarShouldOpen(20, 0.5, 280), true);
  assert.equal(sidebarShouldOpen(20, 0.1, 280), false);
  assert.equal(sidebarShouldClose(-68, 0, 280), true);
  assert.equal(sidebarShouldClose(-20, -0.5, 280), true);
  assert.equal(sidebarShouldClose(-20, -0.1, 280), false);
});

test("reconhece gesto horizontal final e calcula progresso visual", () => {
  assert.equal(sidebarGestureIsHorizontal(42, 1), true);
  assert.equal(sidebarGestureIsHorizontal(20, 1), false);
  assert.equal(sidebarGestureIsHorizontal(60, 100), false);
  assert.equal(sidebarDrawerProgress(false, 0, 280, false), 0);
  assert.equal(sidebarDrawerProgress(false, 0, 280, true), 1);
  assert.equal(sidebarDrawerProgress(true, -140, 280, false), 0.5);
  assert.equal(sidebarDrawerProgress(true, -400, 280, false), 0);
});
