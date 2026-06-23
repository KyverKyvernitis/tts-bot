import { Home } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";

interface SidebarProps {
  modules: DashboardVisualModule[];
  selectedSectionId: string;
  view: "home" | "section";
  onHome(): void;
  onSelect(id: string): void;
}

export function Sidebar({ modules, selectedSectionId, view, onHome, onSelect }: SidebarProps) {
  const main = modules.filter((m) => m.group === "main");
  const system = modules.filter((m) => m.group === "system");

  const renderLink = (m: DashboardVisualModule) => {
    const Icon = m.icon;
    const isActive = view === "section" && selectedSectionId === m.id;
    return (
      <button
        key={m.id}
        className="osk-side-link"
        data-active={isActive}
        data-state={m.enabled === false ? "off" : "neutral"}
        onClick={() => onSelect(m.id)}
      >
        <Icon size={16} />
        <span>{m.label}</span>
        <span className="osk-side-link-status" aria-hidden />
      </button>
    );
  };

  return (
    <aside className="osk-sidebar" aria-label="Navegação do dashboard">
      <div className="osk-brand">
        <div className="osk-brand-mark">OK</div>
        <div className="osk-brand-text">
          <strong>osaka</strong>
          <small>Dashboard</small>
        </div>
      </div>

      <button
        className="osk-side-link"
        data-active={view === "home"}
        onClick={onHome}
      >
        <Home size={16} />
        <span>Início</span>
      </button>

      <div className="osk-side-group">
        <div className="osk-side-group-label">Módulos</div>
        {main.map(renderLink)}
      </div>

      <div className="osk-side-group">
        <div className="osk-side-group-label">Sistema</div>
        {system.map(renderLink)}
      </div>
    </aside>
  );
}
