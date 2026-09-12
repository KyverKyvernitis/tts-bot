import assert from "node:assert/strict";
import test from "node:test";
import { sessionResultFromError } from "../src/app/sessionModel";
import { DashboardHttpError } from "../src/transport/httpClient";

test("401 no bootstrap vira sessão anônima sem falso aviso", () => {
  assert.deepEqual(sessionResultFromError(new DashboardHttpError("x", 401, "http_error", { error: "session_required" })), {
    state: "anonymous",
    user: null,
  });
});

test("falha não autenticacional preserva aviso traduzido", () => {
  assert.deepEqual(sessionResultFromError(new Error("boom")), {
    state: "anonymous",
    user: null,
    notice: { type: "error", text: "boom" },
  });
});
