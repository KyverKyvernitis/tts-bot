import type { DashboardSectionDefinition } from "../../types/dashboard";

export interface ModuleArea { id: string; label: string; groups: string[] }
const area = (id: string, label: string, ...groups: string[]): ModuleArea => ({ id, label, groups });
const AREAS: Record<string, ModuleArea[]> = {
  welcome: [area("entry", "Entrada", "Mensagem de entrada", "Aparência"), area("private", "Mensagem privada", "Mensagem privada"), area("roles", "Cargos", "Cargos")],
  forms: [area("panel", "Painel", "Canais", "Painel"), area("questions", "Perguntas", "Perguntas"), area("responses", "Respostas", "Resposta"), area("approval", "Aprovação", "Aprovação")],
  tickets: [area("panel", "Painel", "Painel"), area("service", "Atendimento", "Atendimento", "Comportamento"), area("flows", "Fluxos", "Fluxos", "Denúncias", "Textos"), area("permissions", "Permissões", "Permissões")],
  color_roles: [area("panels", "Painéis e cores", "Painel"), area("messages", "Mensagens", "Mensagens")],
  birthday: [area("register", "Cadastro", "Geral", "Canais", "Registro de datas"), area("announcements", "Avisos", "Avisos"), area("calendar", "Calendário", "Calendário")],
  tts: [area("voice", "Voz", "Voz"), area("behavior", "Comportamento", "Comportamento"), area("prefixes", "Prefixos", "Prefixos")],
  economy: [area("games", "Jogos do servidor", "Geral", "Permissões")],
};

export const MODULE_ACTIONS: Record<string, string> = {
  welcome: "Enviar boas-vindas", forms: "Receber formulários", tickets: "Receber tickets",
  color_roles: "Oferecer cargos de cor", birthday: "Usar aniversários", tts: "Ler mensagens em voz",
};

export function moduleAreasFor(section: DashboardSectionDefinition): ModuleArea[] {
  const groups = [...new Set(section.fields.filter(field => field.group !== "Ativação").map(field => field.group || "Geral"))];
  const configured = (AREAS[section.id] || []).map(item => ({ ...item, groups: item.groups.filter(group => groups.includes(group)) })).filter(item => item.groups.length);
  const assigned = new Set(configured.flatMap(item => item.groups));
  for (const group of groups) if (!assigned.has(group)) configured.push(area(`extra-${configured.length}`, group, group));
  return configured;
}

export function birthdayTime(draft: Record<string, unknown>): string {
  const hour = Number(draft["birthday.announce_hour"] ?? 9);
  const minute = Number(draft["birthday.announce_minute"] ?? 0);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) return "";
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

export function parseBirthdayTime(value: string): [number, number] | null {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return null;
  return value.split(":").map(Number) as [number, number];
}
