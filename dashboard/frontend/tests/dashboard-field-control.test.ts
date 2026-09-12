import assert from "node:assert/strict";
import test from "node:test";
import {
  channelOptionsForField,
  dashboardBooleanStateLabel,
  channelOptionsWithCurrentValue,
  normalizeDashboardFormField,
  roleOptionsWithCurrentValue,
  stringifyDashboardValue,
} from "../src/app/dashboardFieldValues";
import type { DashboardChannelOption, DashboardFieldDefinition, DashboardRoleOption } from "../src/types/dashboard";

function channelField(id = "welcome.channel_id"): DashboardFieldDefinition {
  return { id, label: "Canal", type: "channel", scope: "welcome", path: "channel_id" };
}

function roleField(type: "role" | "role_multi" = "role"): DashboardFieldDefinition {
  return { id: `general.${type}`, label: "Cargo", type, scope: "guild", path: type };
}

test("mantém zero como valor numérico válido", () => {
  assert.equal(stringifyDashboardValue(0), "0");
  assert.equal(stringifyDashboardValue(-1), "-1");
  assert.equal(stringifyDashboardValue(Number.NaN), "");
});

test("inclui fóruns e canais de mídia nos seletores de texto", () => {
  const channels: DashboardChannelOption[] = [
    { id: "1", name: "texto", type: 0, sendable: true },
    { id: "2", name: "fórum", type: 15, sendable: true },
    { id: "3", name: "mídia", type: 16, sendable: true },
    { id: "4", name: "voz", type: 2, connectable: true },
  ];
  assert.deepEqual(channelOptionsForField(channelField(), channels).map((option) => option.value), ["1", "2", "3"]);
});

test("desabilita canal conhecido sem permissão e preserva canal atual indisponível", () => {
  const channels: DashboardChannelOption[] = [
    { id: "1", name: "permitido", type: 0, permissionsKnown: true, sendable: true },
    { id: "2", name: "bloqueado", type: 0, permissionsKnown: true, sendable: false },
  ];
  const options = channelOptionsForField(channelField(), channels);
  assert.equal(options[0].disabled, false);
  assert.equal(options[1].disabled, true);
  assert.match(options[1].hint ?? "", /não pode visualizar ou enviar mensagens/i);

  const preserved = channelOptionsWithCurrentValue(channelField(), "999", channels);
  assert.deepEqual(preserved[0], {
    value: "999",
    label: "Canal 999",
    hint: "Canal atual indisponível para esta configuração",
    disabled: true,
  });
});

test("filtra cargos gerenciados sem perder seleção antiga", () => {
  const roles: DashboardRoleOption[] = [
    { id: "1", name: "Disponível", color: 0x112233, managed: false, assignable: true },
    { id: "2", name: "Integração", color: 0, managed: true, assignable: false },
  ];
  const options = roleOptionsWithCurrentValue(roleField("role_multi"), ["2", "1"], "", roles);
  assert.deepEqual(options.map((option) => option.value), ["2", "1"]);
  assert.match(options[0].hint ?? "", /indisponível/i);
  assert.match(options[1].hint ?? "", /#112233/i);
});

test("normaliza perguntas antigas sem descartar flags e limites", () => {
  assert.deepEqual(normalizeDashboardFormField({
    id: "custom",
    label: "Nome",
    response_label: "Nome público",
    long: true,
    required: false,
    show_in_response: false,
    enabled: false,
    min_length: 3,
    max_length: 777,
  }, 0), {
    id: "custom",
    label: "Nome",
    placeholder: "",
    response_label: "Nome público",
    required: false,
    long: true,
    show_in_response: false,
    enabled: false,
    min_length: 3,
    max_length: 777,
  });

  assert.deepEqual(normalizeDashboardFormField({}, 1), {
    id: "field2",
    label: "Pergunta 2",
    placeholder: "",
    response_label: "Pergunta 2",
    required: true,
    long: false,
    show_in_response: true,
    enabled: true,
    min_length: 0,
    max_length: 120,
  });
});


test("rotula toggles de função no gênero correto", () => {
  assert.equal(dashboardBooleanStateLabel("welcome.enabled", true), "Ativa");
  assert.equal(dashboardBooleanStateLabel("welcome.enabled", false), "Desativada");
  assert.equal(dashboardBooleanStateLabel("welcome.webhook.enabled", true), "Ativado");
  assert.equal(dashboardBooleanStateLabel("welcome.webhook.enabled", false), "Desativado");
});
