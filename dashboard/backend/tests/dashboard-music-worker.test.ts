import assert from "node:assert/strict";
import test from "node:test";
import { dashboardMusicWorkerRecordAvailable } from "../src/services/dashboardMusicWorker.js";

const now = 1_800_000_000;
const offlineAfter = 90;

function worker(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    enabled: true,
    last_heartbeat_at: now - 5,
    roles: ["phone-worker"],
    capabilities: ["music"],
    ...overrides,
  };
}

test("VPS sem worker mantém música indisponível", () => {
  assert.equal(dashboardMusicWorkerRecordAvailable(null, now, offlineAfter), false);
  assert.equal(dashboardMusicWorkerRecordAvailable({}, now, offlineAfter), false);
});

test("worker APK nunca habilita música do dashboard", () => {
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ runtime_kind: "apk" }), now, offlineAfter), false);
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ source: "core-worker-apk-device" }), now, offlineAfter), false);
});

test("heartbeat expirado ou worker desabilitado não conta", () => {
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ last_heartbeat_at: now - 91 }), now, offlineAfter), false);
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ enabled: false }), now, offlineAfter), false);
});

test("Phone Worker online exige capacidades phone-worker e music", () => {
  assert.equal(dashboardMusicWorkerRecordAvailable(worker(), now, offlineAfter), true);
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ roles: [], capabilities: ["music"] }), now, offlineAfter), false);
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ roles: ["phone_worker"], capabilities: ["music"] }), now, offlineAfter), true);
  assert.equal(dashboardMusicWorkerRecordAvailable(worker({ roles: ["phone-worker"], capabilities: [] }), now, offlineAfter), false);
});
