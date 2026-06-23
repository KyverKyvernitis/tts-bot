import type { LucideIcon } from "lucide-react";
import {
  Activity as ActivityIcon,
  Bot,
  Cake,
  ClipboardList,
  DoorOpen,
  LayoutGrid,
  Mic,
  Music,
  Palette,
  Settings,
  ShieldCheck,
  Ticket,
  Trophy,
  Webhook,
} from "lucide-react";

import type {
  DashboardSectionSummary,
} from "./types/dashboard";

export type ModuleGroup = "main" | "system";

export interface ModuleVisualMeta {
  id: string;
  label: string;
  description: string;
  icon: LucideIcon;
  group: ModuleGroup;
  aliases?: string[];
}

export const MODULE_CATALOG: ModuleVisualMeta[] = [
  { id: "general", label: "Geral", description: "Preferências básicas do servidor.", icon: Settings, group: "system", aliases: ["guild"] },
  { id: "welcome", label: "Boas-vindas", description: "Mensagem, canal e cargo automático para novos membros.", icon: DoorOpen, group: "main" },
  { id: "forms", label: "Formulários", description: "Templates, canais e respostas em embed.", icon: ClipboardList, group: "main", aliases: ["form", "formularios"] },
  { id: "tickets", label: "Tickets", description: "Categorias, mensagens e permissões de atendimento.", icon: Ticket, group: "main", aliases: ["ticket"] },
  { id: "color_roles", label: "Cargos de cor", description: "Painel de seleção de cor personalizada.", icon: Palette, group: "main", aliases: ["color-roles", "colors", "roles_color", "colorroles"] },
  { id: "chatbot", label: "Chatbot IA", description: "Canais, perfis e multi-modelo de resposta.", icon: Bot, group: "main", aliases: ["ai", "ia"] },
  { id: "birthday", label: "Aniversários", description: "Canal de parabéns e cargo do dia.", icon: Cake, group: "main", aliases: ["birthdays"] },
  { id: "tts", label: "TTS", description: "Vozes, idiomas e canais de leitura automática.", icon: Mic, group: "main" },
  { id: "music", label: "Música", description: "Filas, controle de DJ e qualidade de áudio.", icon: Music, group: "main" },
  { id: "gincana", label: "Jogos", description: "Crie eventos, divida em equipes e some pontos.", icon: Trophy, group: "main" },
  { id: "permissions", label: "Permissões", description: "Cargos administrativos e acesso ao painel.", icon: ShieldCheck, group: "system", aliases: ["permissoes"] },
  { id: "webhooks", label: "Webhooks", description: "Identidade e envio das mensagens do bot.", icon: Webhook, group: "system", aliases: ["webhook"] },
  { id: "status", label: "Status", description: "Saúde do bot, workers e integrações.", icon: ActivityIcon, group: "system", aliases: ["health"] },
];

export type DashboardVisualModule = DashboardSectionSummary & {
  icon: LucideIcon;
  group: ModuleGroup;
  available: boolean;
};

export function normalizeModuleId(id: string): string {
  return id.replace(/-/g, "_").toLowerCase();
}

export function findModuleMeta(sectionId: string | null | undefined): ModuleVisualMeta | undefined {
  const normalized = normalizeModuleId(sectionId || "");
  return MODULE_CATALOG.find(
    (item) =>
      item.id === normalized ||
      item.aliases?.some((alias) => normalizeModuleId(alias) === normalized),
  );
}

export function mergeDashboardModules(summary: DashboardSectionSummary[]): DashboardVisualModule[] {
  const byNormalizedId = new Map(summary.map((item) => [normalizeModuleId(item.id), item]));
  const used = new Set<string>();
  const catalog = MODULE_CATALOG
    .filter((item) => item.id !== "callkeeper" && item.id !== "call_keeper")
    .map((meta) => {
      const found =
        byNormalizedId.get(meta.id) ??
        meta.aliases?.map((alias) => byNormalizedId.get(normalizeModuleId(alias))).find(Boolean) ??
        null;
      if (found) used.add(normalizeModuleId(found.id));
      return {
        id: found?.id ?? meta.id,
        label: found?.label ?? meta.label,
        emoji: found?.emoji ?? "•",
        description: found?.description ?? meta.description,
        enabled: found?.enabled ?? null,
        configured: found?.configured ?? 0,
        total: found?.total ?? 0,
        status: found?.status ?? "Configurar",
        icon: meta.icon,
        group: meta.group,
        available: Boolean(found),
      } satisfies DashboardVisualModule;
    });

  const extras = summary
    .filter((item) => !used.has(normalizeModuleId(item.id)))
    .map((item) => ({
      ...item,
      icon: findModuleMeta(item.id)?.icon ?? LayoutGrid,
      group: findModuleMeta(item.id)?.group ?? "main",
      available: true,
    } satisfies DashboardVisualModule));

  return [...catalog, ...extras];
}

export function statusClass(
  summary: { enabled: boolean | null; configured: number; total: number } | undefined,
): "ready" | "partial" | "pending" | "off" | "neutral" {
  if (!summary) return "neutral";
  if (summary.enabled === false) return "off";
  if (summary.total <= 0) return "pending";
  if (summary.configured >= summary.total) return "ready";
  if (summary.configured > 0) return "partial";
  return "pending";
}

export function sectionPercent(summary: { configured: number; total: number } | undefined): number {
  if (!summary || summary.total <= 0) return 0;
  return Math.round((summary.configured / summary.total) * 100);
}

export function shortStatusLabel(summary: DashboardSectionSummary | undefined): string {
  if (!summary) return "—";
  if (summary.enabled === false) return "Desativado";
  if (summary.total <= 0) return "Configurar";
  if (summary.configured >= summary.total) return "Pronto";
  if (summary.configured > 0) return `${summary.configured}/${summary.total}`;
  return "Configurar";
}

export function guildInitials(name: string): string {
  return (
    name
      .split(/\s+/)
      .map((part) => part[0])
      .filter(Boolean)
      .slice(0, 2)
      .join("")
      .toUpperCase() || "S"
  );
}
