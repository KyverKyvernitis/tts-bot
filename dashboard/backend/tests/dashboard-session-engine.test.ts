import assert from "node:assert/strict";
import test from "node:test";
import { createDashboardSessionEngine } from "../src/services/dashboardSessionEngine.js";
import {
  DASHBOARD_SESSION_COOKIE,
  dashboardCookieValue,
  dashboardSessionHash,
} from "../src/services/dashboardSessionPrimitives.js";
import type {
  DashboardSessionStore,
  NewStoredDashboardSession,
  StoredDashboardSession,
  StoredDashboardSessionTokenUpdate,
} from "../src/services/dashboardSessionTypes.js";

const SECRET = "dashboard-session-engine-secret-with-24-chars";

class MemorySessionStore implements DashboardSessionStore {
  private nextId = 1;
  readonly records = new Map<string, StoredDashboardSession>();
  deleteCount = 0;
  updateCount = 0;

  async insert(session: NewStoredDashboardSession) {
    this.records.set(session.sessionHash, { ...session, id: this.nextId++ });
  }

  async findByHash(sessionHash: string) {
    return this.records.get(sessionHash) ?? null;
  }

  async deleteById(id: unknown) {
    for (const [hash, record] of this.records) {
      if (record.id === id) {
        this.records.delete(hash);
        this.deleteCount += 1;
        return;
      }
    }
  }

  async deleteByHash(sessionHash: string) {
    if (this.records.delete(sessionHash)) this.deleteCount += 1;
  }

  async updateTokens(id: unknown, update: StoredDashboardSessionTokenUpdate) {
    for (const [hash, record] of this.records) {
      if (record.id === id) {
        this.records.set(hash, { ...record, ...update });
        this.updateCount += 1;
        return true;
      }
    }
    return false;
  }
}

function cookieHeader(setCookie: string): string {
  return setCookie.split(";", 1)[0];
}

function sessionHashFromCookie(setCookie: string): string {
  const raw = dashboardCookieValue(cookieHeader(setCookie), DASHBOARD_SESSION_COOKIE);
  return dashboardSessionHash(raw);
}

test("engine cria e recupera sessão sem depender do MongoDB", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({ ok: false, accessToken: null, error: "unused", detail: null }),
  });
  const created = await engine.createSession({
    ok: true,
    accessToken: "access-token",
    refreshToken: "refresh-token",
    expiresIn: 3600,
    error: null,
    detail: null,
  }, true);

  assert.match(created.setCookie, /^osk_dashboard_session=/);
  assert.match(created.setCookie, /Secure/);
  assert.equal(store.records.size, 1);
  const loaded = await engine.getSession(cookieHeader(created.setCookie));
  assert.equal(loaded?.id, created.session.id);
  assert.equal(loaded?.accessToken, "access-token");
  assert.equal(loaded?.refreshToken, "refresh-token");
});

test("sessão expirada ou criptografia inválida é descartada", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({ ok: false, accessToken: null, error: "unused", detail: null }),
  });
  const expired = await engine.createSession({ ok: true, accessToken: "a", expiresIn: 3600, error: null, detail: null }, false);
  const expiredHash = sessionHashFromCookie(expired.setCookie);
  store.records.get(expiredHash)!.expiresAt = new Date(0);
  assert.equal(await engine.getSession(cookieHeader(expired.setCookie)), null);
  assert.equal(store.records.has(expiredHash), false);

  const corrupt = await engine.createSession({ ok: true, accessToken: "b", expiresIn: 3600, error: null, detail: null }, false);
  const corruptHash = sessionHashFromCookie(corrupt.setCookie);
  store.records.get(corruptHash)!.encryptedAccessToken = "invalid";
  assert.equal(await engine.getSession(cookieHeader(corrupt.setCookie)), null);
  assert.equal(store.records.has(corruptHash), false);
});

test("sessão prestes a expirar sem refresh token é invalidada", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({ ok: false, accessToken: null, error: "unused", detail: null }),
  });
  const created = await engine.createSession({ ok: true, accessToken: "a", expiresIn: 3600, error: null, detail: null }, false);
  const hash = sessionHashFromCookie(created.setCookie);
  store.records.get(hash)!.accessExpiresAt = new Date(Date.now() + 1000);

  assert.equal(await engine.getSession(cookieHeader(created.setCookie)), null);
  assert.equal(store.records.has(hash), false);
});

test("refresh concorrente é single-flight e preserva refresh token quando Discord não rotaciona", async () => {
  const store = new MemorySessionStore();
  let refreshCalls = 0;
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async (refreshToken) => {
      refreshCalls += 1;
      await Promise.resolve();
      assert.equal(refreshToken, "refresh-old");
      return { ok: true, accessToken: "access-new", refreshToken: null, expiresIn: 3600, error: null, detail: null };
    },
  });
  const created = await engine.createSession({
    ok: true,
    accessToken: "access-old",
    refreshToken: "refresh-old",
    expiresIn: 3600,
    error: null,
    detail: null,
  }, false);
  const hash = sessionHashFromCookie(created.setCookie);
  store.records.get(hash)!.accessExpiresAt = new Date(Date.now() + 1000);
  const header = cookieHeader(created.setCookie);

  const [first, second] = await Promise.all([engine.getSession(header), engine.getSession(header)]);
  assert.equal(refreshCalls, 1);
  assert.equal(store.updateCount, 1);
  assert.equal(first?.accessToken, "access-new");
  assert.equal(second?.accessToken, "access-new");
  assert.equal(first?.refreshToken, "refresh-old");
  assert.equal(second?.refreshToken, "refresh-old");
});

test("falha de refresh remove a sessão", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({ ok: false, accessToken: null, error: "refresh_failed", detail: null }),
  });
  const created = await engine.createSession({
    ok: true,
    accessToken: "access-old",
    refreshToken: "refresh-old",
    expiresIn: 3600,
    error: null,
    detail: null,
  }, false);
  const hash = sessionHashFromCookie(created.setCookie);
  store.records.get(hash)!.accessExpiresAt = new Date(Date.now() + 1000);

  assert.equal(await engine.getSession(cookieHeader(created.setCookie)), null);
  assert.equal(store.records.has(hash), false);
});

test("destroy remove pelo hash do cookie e ignora cookie ausente", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({ ok: false, accessToken: null, error: "unused", detail: null }),
  });
  const created = await engine.createSession({ ok: true, accessToken: "a", expiresIn: 3600, error: null, detail: null }, false);
  const hash = sessionHashFromCookie(created.setCookie);
  await engine.destroySession(undefined);
  assert.equal(store.records.has(hash), true);
  await engine.destroySession(cookieHeader(created.setCookie));
  assert.equal(store.records.has(hash), false);
});

test("refresh rotacionado substitui o token persistido e retornado", async () => {
  const store = new MemorySessionStore();
  const engine = createDashboardSessionEngine({
    secret: SECRET,
    store,
    refreshDiscordToken: async () => ({
      ok: true,
      accessToken: "access-rotated",
      refreshToken: "refresh-new",
      expiresIn: 7200,
      error: null,
      detail: null,
    }),
  });
  const created = await engine.createSession({
    ok: true,
    accessToken: "access-old",
    refreshToken: "refresh-old",
    expiresIn: 3600,
    error: null,
    detail: null,
  }, false);
  const hash = sessionHashFromCookie(created.setCookie);
  store.records.get(hash)!.accessExpiresAt = new Date(Date.now() + 1000);

  const refreshed = await engine.getSession(cookieHeader(created.setCookie));
  assert.equal(refreshed?.accessToken, "access-rotated");
  assert.equal(refreshed?.refreshToken, "refresh-new");
  assert.equal(store.updateCount, 1);

  const second = await engine.getSession(cookieHeader(created.setCookie));
  assert.equal(second?.accessToken, "access-rotated");
  assert.equal(second?.refreshToken, "refresh-new");
});
