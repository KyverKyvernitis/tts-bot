import assert from "node:assert/strict";
import test from "node:test";
import { accountIdentityName, accountMenuPosition, adjacentAccountMenuIndex } from "../src/components/account-menu/accountMenuModel";

test("nome da conta prioriza global name, depois username e fallback", () => {
  assert.equal(accountIdentityName({ id: "1", global_name: " Osaka ", username: "user" }), "Osaka");
  assert.equal(accountIdentityName({ id: "1", global_name: " ", username: " user " }), "user");
  assert.equal(accountIdentityName({ id: "1" }), "Conta");
});

test("posição mantém margem e altura mínima", () => {
  assert.deepEqual(accountMenuPosition({ bottom: 100, right: 760 }, 800, 700), { top: 108, right: 40, maxHeight: 584 });
  assert.deepEqual(accountMenuPosition({ bottom: 680, right: 799 }, 800, 700), { top: 688, right: 8, maxHeight: 152 });
});

test("navegação de teclado circula entre itens", () => {
  assert.equal(adjacentAccountMenuIndex(-1, 4, 1), 0);
  assert.equal(adjacentAccountMenuIndex(-1, 4, -1), 3);
  assert.equal(adjacentAccountMenuIndex(3, 4, 1), 0);
  assert.equal(adjacentAccountMenuIndex(0, 4, -1), 3);
  assert.equal(adjacentAccountMenuIndex(0, 0, 1), -1);
});
