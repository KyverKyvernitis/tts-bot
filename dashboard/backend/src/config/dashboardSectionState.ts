function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isConfiguredValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value > 0;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (isPlainObject(value)) return Object.keys(value).length > 0;
  return true;
}

export type DashboardSectionState = {
  enabled: boolean | null;
  state: "active" | "inactive";
  status: string;
  issues: string[];
};

function hasValue(values: Record<string, unknown>, fieldId: string): boolean {
  return isConfiguredValue(values[fieldId]);
}

function binarySectionState(enabled: boolean, issues: string[]): DashboardSectionState {
  const active = enabled && issues.length === 0;
  return { enabled, state: active ? "active" : "inactive", status: active ? "Ativa" : "Desativada", issues };
}

export function resolveDashboardSectionState(sectionId: string, values: Record<string, unknown>): DashboardSectionState {
  if (sectionId === "welcome") {
    const enabled = Boolean(values["welcome.enabled"]);
    const issues = hasValue(values, "welcome.channel_id") ? [] : ["Selecione o canal de boas-vindas."];
    return binarySectionState(enabled, issues);
  }

  if (sectionId === "birthday") {
    const enabled = Boolean(values["birthday.enabled"]);
    const register = hasValue(values, "birthday.register_channel_id");
    const announce = hasValue(values, "birthday.announce_channel_id");
    const issues = register || announce ? [] : ["Selecione um canal de cadastro ou de avisos."];
    return binarySectionState(enabled, issues);
  }

  if (sectionId === "forms") {
    const enabled = Boolean(values["forms.enabled"]);
    const formChannel = hasValue(values, "forms.form_channel_id");
    const responseChannel = hasValue(values, "forms.responses_channel_id");
    const issues: string[] = [];
    if (!formChannel) issues.push("Selecione o canal do formulário.");
    if (!responseChannel) issues.push("Selecione o canal de respostas.");
    return binarySectionState(enabled, issues);
  }

  if (sectionId === "tickets") {
    const enabled = Boolean(values["tickets.feature_enabled"]);
    const panel = hasValue(values, "tickets.panel.channel_id");
    const enabledFlow = ["partnership", "report", "suggestion", "other"].some((flow) => Boolean(values[`tickets.option_items.${flow}.enabled`] ?? values[`tickets.enabled.${flow}`]));
    const issues: string[] = [];
    if (!panel) issues.push("Selecione o canal do painel.");
    if (!enabledFlow) issues.push("Ative pelo menos um fluxo de atendimento.");
    return binarySectionState(enabled, issues);
  }

  if (sectionId === "color_roles") {
    const enabled = Boolean(values["color_roles.enabled"]);
    const panel = hasValue(values, "color_roles.channel_id");
    const issues: string[] = [];
    if (!panel) issues.push("Selecione o canal dos painéis.");
    return binarySectionState(enabled, issues);
  }

  if (sectionId === "tts") return binarySectionState(Boolean(values["tts.enabled"]), []);
  return { enabled: null, state: "inactive", status: "", issues: [] };
}
