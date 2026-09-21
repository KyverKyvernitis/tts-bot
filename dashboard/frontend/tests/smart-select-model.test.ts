import assert from "node:assert/strict";
import test from "node:test";
import { anchoredSelectPosition } from "../src/components/smart-select/anchoredSelectPosition";
import {
  adjacentEnabledSmartSelectIndex,
  filterSmartSelectOptions,
  firstEnabledSmartSelectIndex,
  smartSelectEstimatedHeight,
  smartSelectPosition,
  type SmartSelectOption,
} from "../src/components/smart-select/smartSelectModel";

const options: SmartSelectOption[] = [
  { value: "a", label: "Alpha", hint: "Primeiro" },
  { value: "b", label: "Beta", disabled: true },
  { value: "c", label: "Gamma", hint: "Terceiro" },
];

test("filtro busca label e hint sem alterar a lista vazia", () => {
  assert.equal(filterSmartSelectOptions(options, ""), options);
  assert.deepEqual(filterSmartSelectOptions(options, "terceiro").map((item) => item.value), ["c"]);
  assert.deepEqual(filterSmartSelectOptions(options, "ALPHA").map((item) => item.value), ["a"]);
});

test("navegação pula opções desabilitadas e respeita os limites", () => {
  assert.equal(firstEnabledSmartSelectIndex(options), 0);
  assert.equal(adjacentEnabledSmartSelectIndex(options, 0, 1), 2);
  assert.equal(adjacentEnabledSmartSelectIndex(options, 2, -1), 0);
  assert.equal(adjacentEnabledSmartSelectIndex(options, 2, 1), 2);
  assert.equal(adjacentEnabledSmartSelectIndex([], -1, 1), -1);
});

test("altura estimada preserva mínimos e máximos do popover", () => {
  assert.equal(smartSelectEstimatedHeight(1), 220);
  assert.equal(smartSelectEstimatedHeight(9), 420);
  assert.equal(smartSelectEstimatedHeight(50), 420);
});

test("posição escolhe acima quando falta espaço e mantém margens", () => {
  assert.deepEqual(
    smartSelectPosition({ left: 100, right: 300, top: 500, bottom: 540, width: 200 }, 800, 600, 250),
    { left: 100, top: 243, width: 220, placement: "above" },
  );
  assert.deepEqual(
    smartSelectPosition({ left: -10, right: 110, top: 40, bottom: 80, width: 120 }, 300, 700, 220),
    { left: 8, top: 87, width: 220, placement: "below" },
  );
});

test("seletor de servidor permanece junto ao botão em telas estreitas", () => {
  for (const width of [320, 390, 736, 1024]) {
    const rect = { left: 16, right: width - 16, top: 80, bottom: 132, width: width - 32 };
    const position = anchoredSelectPosition(rect, { left: 0, top: 0, width, height: 780 }, 172);
    assert.equal(position.top, rect.bottom + 7);
    assert.equal(position.left, 16);
    assert.equal(position.width, rect.width);
    assert.equal(position.placement, "below");
    assert.ok(position.left + position.width <= width - 8);
  }
});

test("menu de servidor vira acima e respeita o espaço disponível com teclado", () => {
  const rect = { left: 290, right: 630, top: 720, bottom: 772, width: 340 };
  const flipped = anchoredSelectPosition(rect, { left: 0, top: 0, width: 390, height: 780 }, 172);
  assert.equal(flipped.placement, "above");
  assert.equal(flipped.top + 172, rect.top - 7);
  assert.ok(flipped.left + flipped.width <= 382);
  const viewport = { left: 12, top: 100, width: 320, height: 250 };
  const cramped = anchoredSelectPosition({ left: 5, right: 505, top: 145, bottom: 197, width: 500 }, viewport, 420);
  assert.ok(cramped.left >= 20);
  assert.ok(cramped.left + cramped.width <= 324);
  assert.ok(cramped.top + cramped.maxHeight <= 342);
});
