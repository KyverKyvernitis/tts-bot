import assert from "node:assert/strict";
import test from "node:test";
import { runSingleFlight } from "../src/services/singleFlight.js";

test("reutiliza a mesma promise para a mesma chave enquanto o trabalho está em andamento", async () => {
  const flights = new Map<string, Promise<number>>();
  let calls = 0;
  let release!: (value: number) => void;
  const factory = () => {
    calls += 1;
    return new Promise<number>((resolve) => { release = resolve; });
  };

  const first = runSingleFlight(flights, "session", factory);
  const second = runSingleFlight(flights, "session", factory);
  assert.equal(first, second);
  assert.equal(calls, 0, "factory deve começar no microtask criado pela single-flight");
  await Promise.resolve();
  assert.equal(calls, 1);
  release(42);
  assert.deepEqual(await Promise.all([first, second]), [42, 42]);
  await Promise.resolve();
  assert.equal(flights.has("session"), false);
});

test("limpa rejeições e permite nova tentativa posterior", async () => {
  const flights = new Map<string, Promise<number>>();
  let calls = 0;
  await assert.rejects(runSingleFlight(flights, "session", async () => {
    calls += 1;
    throw new Error("boom");
  }), /boom/);
  await Promise.resolve();
  assert.equal(flights.has("session"), false);
  const value = await runSingleFlight(flights, "session", async () => {
    calls += 1;
    return 7;
  });
  assert.equal(value, 7);
  assert.equal(calls, 2);
});

test("chaves diferentes não bloqueiam umas às outras", async () => {
  const flights = new Map<string, Promise<string>>();
  const result = await Promise.all([
    runSingleFlight(flights, "a", async () => "A"),
    runSingleFlight(flights, "b", async () => "B"),
  ]);
  assert.deepEqual(result, ["A", "B"]);
});
