import assert from "node:assert/strict";
import test from "node:test";
import { dashboardWorkerRecordAvailable } from "../src/services/dashboardWorkerAvailability.js";

const now = 1_800_000_000;
const spec = {
  required_capabilities: ["phone-worker", "demo-capability"],
  excluded_runtime_kinds: ["apk"],
  excluded_source_prefixes: ["core-worker-apk"],
  offline_after_seconds: 90,
};

function worker(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    enabled: true,
    last_heartbeat_at: now - 5,
    roles: ["phone-worker"],
    capabilities: ["demo-capability"],
    ...overrides,
  };
}

test("worker ausente, desabilitado ou expirado não fica disponível", () => {
  assert.equal(dashboardWorkerRecordAvailable(null, now, spec), false);
  assert.equal(dashboardWorkerRecordAvailable(worker({ enabled: false }), now, spec), false);
  assert.equal(dashboardWorkerRecordAvailable(worker({ last_heartbeat_at: now - 91 }), now, spec), false);
});

test("runtime e source excluídos pela especificação são rejeitados", () => {
  assert.equal(dashboardWorkerRecordAvailable(worker({ runtime_kind: "apk" }), now, spec), false);
  assert.equal(dashboardWorkerRecordAvailable(worker({ source: "core-worker-apk-device" }), now, spec), false);
});

test("todas as capabilities exigidas precisam estar presentes", () => {
  assert.equal(dashboardWorkerRecordAvailable(worker(), now, spec), true);
  assert.equal(dashboardWorkerRecordAvailable(worker({ roles: [], capabilities: ["demo-capability"] }), now, spec), false);
  assert.equal(dashboardWorkerRecordAvailable(worker({ roles: ["phone_worker"] }), now, spec), true);
});
