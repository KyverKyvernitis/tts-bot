import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ServerSwitcher } from "../src/components/ServerSwitcher";
import { mergeDashboardModules, MODULE_CATALOG } from "../src/moduleCatalog";
import { channelOptionsForField, roleOptionsWithCurrentValue } from "../src/app/dashboardFieldValues";
import type { DashboardFieldDefinition, DashboardServerCard } from "../src/types/dashboard";

const server = (id: string, name: string): DashboardServerCard => ({ id, name, icon: null, canManage: true, botPresent: true, canInvite: false, owner: true, permissions: "8", reason: "owner" });
const one = server("123456789012345678", "Comunidade Osaka");
const two = server("123456789012345679", "Servidor de testes");
const field = (id: string, type: "channel" | "role"): DashboardFieldDefinition => ({ id, label: id, type, scope: "guild", path: id });

test("Economia / Jogos fica por último entre os módulos retornados pelo backend", () => {
  const summary = [...MODULE_CATALOG].reverse().map((item) => ({ ...item, emoji: "", enabled: null, configured: 0, total: 0, status: "" }));
  const modules = mergeDashboardModules(summary).filter((item) => item.group === "main");
  assert.equal(modules.length, 7);
  assert.equal(modules.at(-1)?.id, "economy");
  assert.equal(modules.at(-1)?.label, "Economia / Jogos");
  assert.equal(mergeDashboardModules(summary.filter((item) => item.id !== "economy")).some((item) => item.id === "economy"), false);
});

test("seletor usa o dropdown contextual apenas com mais de um servidor autorizado", () => {
  const render = (servers: DashboardServerCard[]) => renderToStaticMarkup(React.createElement(ServerSwitcher, { guildId: one.id, guildName: one.name, servers, onSelect() {} }));
  assert.doesNotMatch(render([one]), /aria-haspopup/);
  assert.doesNotMatch(render([one, { ...two, botPresent: false }]), /aria-haspopup/);
  assert.doesNotMatch(render([one, { ...two, canManage: false }]), /aria-haspopup/);
  const html = render([one, two]);
  assert.match(html, /data-presentation="anchored"/);
  assert.match(html, /aria-label="Escolher servidor"/);
  assert.match(html, /Comunidade Osaka/);
});

test("economia oferece os mesmos tipos de canal aceitos pelo painel do bot", () => {
  const channels = [0, 2, 4, 5, 13, 15, 16].map((type) => ({ id: String(type), name: `canal-${type}`, type }));
  const options = channelOptionsForField(field("economy.channel_id", "channel"), channels);
  assert.deepEqual(options.map((item) => item.value), ["0", "2", "5", "13"]);
});

test("cargo de staff pode estar acima do bot sem liberar atribuição em outros módulos", () => {
  const roles = [{ id: "1", name: "Equipe", managed: false, assignable: false }];
  assert.deepEqual(roleOptionsWithCurrentValue(field("economy.staff_role_id", "role"), "", "", roles).map((item) => item.value), ["1"]);
  assert.deepEqual(roleOptionsWithCurrentValue(field("welcome.role_id", "role"), "", "", roles), []);
});
