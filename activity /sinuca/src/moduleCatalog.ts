import type { LucideIcon } from "lucide-react";
import {
  Cake,
  Cpu,
  DoorOpen,
  HardDrive,
  LayoutGrid,
  Mic,
  Music,
  Settings,
  Ticket,
  UploadCloud,
} from "lucide-react";

import type { DashboardSectionSummary } from "./types/dashboard";

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
  { id: "welcome", label: "Boas-vindas", description: "Mensagem, canal e cargo automático para novos membros.", icon: DoorOpen, group: "main" },
  { id: "tickets", label: "Tickets", description: "Categorias, mensagens e permissões de atendimento.", icon: Ticket, group: "main", aliases: ["ticket"] },
  { id: "birthday", label: "Aniversários", description: "Canal de parabéns, calendário público e cargo do dia.", icon: Cake, group: "main", aliases: ["birthdays", "aniversarios"] },
  { id: "music", label: "Música", description: "Fila, permissões de DJ, volume e comportamento do player.", icon: Music, group: "main" },
  { id: "tts", label: "TTS", description: "Vozes, idiomas, canais e limites de leitura automática.", icon: Mic, group: "main" },
  { id: "workers", label: "Workers", description: "Processos em segundo plano, filas e capacidade dos workers.", icon: Cpu, group: "main", aliases: ["worker", "jobs", "queue"] },
  { id: "updates", label: "Updates", description: "Canal de update, avisos e estado do auto update.", icon: UploadCloud, group: "main", aliases: ["update", "releases", "changelog"] },
  { id: "vps", label: "VPS", description: "Recursos do servidor, status e monitoramento da máquina.", icon: HardDrive, group: "main", aliases: ["server_host", "host", "machine"] },
  { id: "general", label: "Configurações", description: "Preferências básicas do servidor e do painel.", icon: Settings, group: "system", aliases: ["guild", "config", "settings", "configuracoes"] },
];

export type DashboardVisualModule = DashboardSectionSummary & {
  icon: LucideIcon;
  group: ModuleGroup;
  available: boolean;
};

export function normalizeModuleId(id: string): string {
  return id.replace(/-/g, "_").toLowerCase();
}

function isHiddenModule(id: string): boolean {
  const normalized = normalizeModuleId(id);
  return ["callkeeper", "call_keeper", "sinuca", "pool", "bilhar", "game", "games", "jogo", "jogos", "gincana"].some((part) => normalized.includes(part));
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
  const visibleSummary = summary.filter((item) => !isHiddenModule(item.id) && !isHiddenModule(item.label));
  const byNormalizedId = new Map(visibleSummary.map((item) => [normalizeModuleId(item.id), item]));
  const used = new Set<string>();
  const catalog = MODULE_CATALOG.map((meta) => {
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

  const extras = visibleSummary
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
