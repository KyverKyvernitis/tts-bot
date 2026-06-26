import { ChevronRight, HelpCircle, LayoutGrid } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface HomePageProps {
  guildName: string;
  modules: DashboardVisualModule[];
  onOpen(id: string): void;
}

const HERO_CHIPS = ["TTS", "Chatbot", "Boas-vindas", "Cores", "Tickets"];

export function HomePage({ guildName, modules, onOpen }: HomePageProps) {
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
              Ajuste mensagens, canais, permissões e preferências do servidor.
            </p>
          </div>
          <button className="osk-icon-btn" aria-label="Ajuda">
            <HelpCircle size={16} />
          </button>
        </div>

        <div className="osk-hero-chips">
          {HERO_CHIPS.map((chip) => (
            <span key={chip} className="osk-chip">
              {chip}
            </span>
          ))}
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
