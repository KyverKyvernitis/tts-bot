import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createDashboardEdgeVoiceLoader, parseDashboardEdgeVoices } from "../src/services/dashboardEdgeVoiceCatalog.js";

test("catálogo reaproveita ShortNames de todos os idiomas, variantes e nomes únicos", () => {
  const voices = parseDashboardEdgeVoices({ voices: ["pt-BR-FranciscaNeural", "en-US-AriaNeural", "sr-Latn-RS-NicholasNeural", "zh-CN-liaoning-XiaobeiNeural", "en-US-AriaNeural", "../arquivo", null] });
  assert.equal(voices.length, 4);
  assert.equal(voices.find(v => v.value === "sr-Latn-RS-NicholasNeural")?.locale, "sr-Latn-RS");
  assert.equal(voices.find(v => v.value === "zh-CN-liaoning-XiaobeiNeural")?.locale, "zh-CN-liaoning");
  assert.equal(voices.find(v => v.value === "en-US-AriaNeural")?.label, "Aria");
  assert.deepEqual(parseDashboardEdgeVoices({ voices: "invalid" }), []);
});

test("leitura usa cache, atualiza sob demanda e preserva o último catálogo em falhas", async () => {
  const dir = await mkdtemp(join(tmpdir(), "osaka-edge-"));
  const path = join(dir, "edge-voices.json");
  let now = 1000;
  const load = createDashboardEdgeVoiceLoader(() => path, () => now);
  try {
    assert.equal((await load()).status, "unavailable");
    await writeFile(path, JSON.stringify({ voices: ["en-US-AriaNeural"] }));
    const first = await load(true);
    assert.equal(first.status, "ready");
    await writeFile(path, JSON.stringify({ voices: ["ja-JP-NanamiNeural"] }));
    assert.equal((await load()).voices[0].value, "en-US-AriaNeural");
    now += 60_001;
    const [next, concurrent] = await Promise.all([load(), load()]);
    assert.deepEqual(next, concurrent);
    assert.equal(next.voices[0].value, "ja-JP-NanamiNeural");
    await writeFile(path, "{incomplete");
    const fallback = await load(true);
    assert.equal(fallback.status, "cached");
    assert.deepEqual(fallback.voices, next.voices);
    await rm(path);
    assert.deepEqual((await load(true)).voices, next.voices);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
