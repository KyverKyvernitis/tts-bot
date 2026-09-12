import assert from "node:assert/strict";
import test from "node:test";
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
