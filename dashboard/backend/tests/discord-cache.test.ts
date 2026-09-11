import assert from "node:assert/strict";
import test from "node:test";
import {
  getShortCacheValue,
  setShortCacheValue,
  tokenCacheKey,
  type ShortCacheEntry,
} from "../src/services/discordCache.js";

test("hash do token é determinístico sem armazenar o segredo em claro", () => {
  const key = tokenCacheKey("segredo");
  assert.equal(key, tokenCacheKey("segredo"));
  assert.notEqual(key, tokenCacheKey("outro"));
  assert.equal(key.includes("segredo"), false);
});

test("cache retorna entradas válidas e remove entradas expiradas", () => {
  const cache = new Map<string, ShortCacheEntry<number>>();
  setShortCacheValue(cache, "a", 1, 100, { now: 1_000 });
  assert.equal(getShortCacheValue(cache, "a", 1_050), 1);
  assert.equal(getShortCacheValue(cache, "a", 1_100), null);
  assert.equal(cache.has("a"), false);
});

test("cache remove expirados antes de expulsar a entrada mais antiga", () => {
  const cache = new Map<string, ShortCacheEntry<number>>([
    ["expired", { expiresAt: 10, value: 0 }],
    ["live", { expiresAt: 10_000, value: 1 }],
  ]);
  setShortCacheValue(cache, "new", 2, 100, { now: 100, maxEntries: 2 });
  assert.deepEqual([...cache.keys()], ["live", "new"]);
});

test("cache respeita limite máximo removendo a entrada mais antiga", () => {
  const cache = new Map<string, ShortCacheEntry<number>>();
  setShortCacheValue(cache, "first", 1, 1_000, { now: 0, maxEntries: 2 });
  setShortCacheValue(cache, "second", 2, 1_000, { now: 0, maxEntries: 2 });
  setShortCacheValue(cache, "third", 3, 1_000, { now: 0, maxEntries: 2 });
  assert.deepEqual([...cache.keys()], ["second", "third"]);
});
