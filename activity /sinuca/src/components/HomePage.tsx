import { ChevronRight, HelpCircle, LayoutGrid } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  guildName: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function HomePage({ guildName, modules, onOpen }: HomePageProps) {
  const main = modules.filter((m) => m.group === "main");
  const system = modules.filter((m) => m.group === "system");
  const totalOptions = modules.reduce((acc, m) => acc + m.total, 0);

  return (
    <section className="osk-page">
      <div className="osk-hero">
        <div className="osk-hero-head">
          <div>
            <span className="osk-hero-eyebrow">Painel · {guildName}</span>
            <h1>Configure cada módulo do bot em um lugar.</h1>
            <p>
              Ajuste recursos, mensagens, canais e permissões do servidor sem sair do painel.
            </p>
          </div>
          <button className="osk-icon-btn" aria-label="Ajuda">
            <HelpCircle size={16} />
          </button>
        </div>

        <div className="osk-hero-stats">
          <div className="osk-stat">
            <span className="osk-stat-label">Módulos</span>
            <span className="osk-stat-value">{modules.length}</span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Principais</span>
            <span className="osk-stat-value">{main.length}</span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Sistema</span>
            <span className="osk-stat-value">{system.length}</span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Opções</span>
            <span className="osk-stat-value">{totalOptions}</span>
          </div>
        </div>

        <div className="osk-hero-accent" aria-hidden />
      </div>

      <ModuleGrid label="Módulos principais" modules={main} onOpen={onOpen} />
      <ModuleGrid label="Sistema" modules={system} onOpen={onOpen} />
    </section>
  );
}

function ModuleGrid({
  label,
  modules,
  onOpen,
}: {
  label: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}) {
  if (!modules.length) return null;
  return (
    <>
      <div className="osk-section-title">
        <LayoutGrid size={14} color="var(--osk-muted)" />
        <h2>{label}</h2>
        <span className="osk-section-title-count">
          {modules.length} módulos
        </span>
        <span className="osk-section-title-line" />
      </div>
      <div className="osk-module-grid">
        {modules.map((m, idx) => (
          <ModuleCard key={m.id} module={m} index={idx} onOpen={onOpen} />
        ))}
      </div>
    </>
  );
}

function ModuleCard({
  module: m,
  index,
  onOpen,
}: {
  module: DashboardVisualModule;
  index: number;
  onOpen(id: string): void;
}) {
  const Icon = m.icon;
  const state = m.enabled === false ? "off" : "neutral";
  return (
    <button
      className="osk-module-card"
      data-state={state}
      style={{ animationDelay: `${index * 24}ms` }}
      onClick={() => onOpen(m.id)}
    >
      <span className="osk-module-icon">
        <Icon size={20} />
      </span>
      <span className="osk-module-body">
        <span className="osk-module-head">
          <strong>{m.label}</strong>
        </span>
        <span className="osk-module-desc">{m.description}</span>
      </span>
      <ChevronRight size={18} className="osk-module-chev" />
    </button>
  );
}
