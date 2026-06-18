import { ChevronRight, HelpCircle, LayoutGrid } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";
import { sectionPercent, shortStatusLabel, statusClass } from "../moduleCatalog";

interface HomePageProps {
  guildName: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

export function HomePage({ guildName, modules, onOpen }: HomePageProps) {
  const totalFields = modules.reduce((acc, m) => acc + m.total, 0);
  const configured = modules.reduce((acc, m) => acc + m.configured, 0);
  const readySections = modules.filter((m) => m.total > 0 && m.configured >= m.total).length;
  const activeModules = modules.filter((m) => m.enabled !== false).length;
  const percent = totalFields > 0 ? Math.round((configured / totalFields) * 100) : 0;

  const main = modules.filter((m) => m.group === "main");
  const system = modules.filter((m) => m.group === "system");

  return (
    <section className="osk-page">
      <div className="osk-hero">
        <div className="osk-hero-head">
          <div>
            <span className="osk-hero-eyebrow">Painel · {guildName}</span>
            <h1>Configure cada módulo do bot em um lugar.</h1>
            <p>
              Toque em um cartão para abrir as opções. Os indicadores mostram o que já está pronto,
              o que precisa de atenção e o que está desligado.
            </p>
          </div>
          <button className="osk-icon-btn" aria-label="Ajuda">
            <HelpCircle size={16} />
          </button>
        </div>

        <div className="osk-hero-stats">
          <div className="osk-stat">
            <span className="osk-stat-label">Configurado</span>
            <span className="osk-stat-value">
              {percent}
              <sup>%</sup>
            </span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Módulos prontos</span>
            <span className="osk-stat-value">
              {readySections}
              <sup>/ {modules.length}</sup>
            </span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Ativos</span>
            <span className="osk-stat-value">{activeModules}</span>
          </div>
          <div className="osk-stat">
            <span className="osk-stat-label">Campos</span>
            <span className="osk-stat-value">
              {configured}
              <sup>/ {totalFields}</sup>
            </span>
          </div>
        </div>

        <div className="osk-progress" aria-label="Progresso geral">
          <div className="osk-progress-bar" style={{ width: `${percent}%` }} />
        </div>
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
  const ready = modules.filter((m) => m.total > 0 && m.configured >= m.total).length;
  return (
    <>
      <div className="osk-section-title">
        <LayoutGrid size={14} color="var(--osk-muted)" />
        <h2>{label}</h2>
        <span className="osk-section-title-count">
          {ready}/{modules.length} prontos
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
  const state = statusClass(m);
  const percent = sectionPercent(m);
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
          <span className="osk-badge" data-state={state}>
            {shortStatusLabel(m)}
          </span>
        </span>
        <span className="osk-module-desc">{m.description}</span>
        {m.total > 0 && (
          <span className="osk-module-meter" aria-hidden>
            <span style={{ width: `${percent}%` }} />
          </span>
        )}
      </span>
      <ChevronRight size={18} className="osk-module-chev" />
    </button>
  );
}
