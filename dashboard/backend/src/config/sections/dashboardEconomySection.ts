import type { DashboardSectionDefinition } from "../dashboardTypes.js";

export const economySection: DashboardSectionDefinition = {
  id: "economy", label: "Economia / Jogos", emoji: "♠️", description: "Preferências dos jogos com fichas neste servidor.",
  groups: ["Geral", "Permissões"],
  fields: [
    {
      id: "economy.channel_id", label: "Canal dos jogos", type: "channel", scope: "guild", path: "gincana_channel_id", group: "Geral",
      description: "Selecione Nenhum para permitir os jogos em qualquer canal compatível.",
    },
    {
      id: "economy.input_mode", label: "Como iniciar os jogos", type: "select", scope: "guild", path: "gincana_input_mode", group: "Geral",
      description: "Comandos com prefixo continuam disponíveis nos dois modos.",
      options: [{ value: "triggers", label: "Palavras no chat e comandos" }, { value: "commands", label: "Somente comandos com prefixo" }],
    },
    {
      id: "economy.staff_role_id", label: "Cargo da equipe de economia", type: "role", scope: "guild", path: "gincana_staff_role_id", group: "Permissões",
      description: "Cargo autorizado a administrar a economia e as fichas, conforme as permissões do bot.",
    },
  ],
};
