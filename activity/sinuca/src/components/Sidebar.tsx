import { Home, LogOut, X } from "lucide-react";
import type { DashboardVisualModule } from "../moduleCatalog";
import { Brand } from "./BrowserLanding";

interface SidebarProps {
  modules: DashboardVisualModule[];
  selectedSectionId: string;
  view: "home" | "section";
  mobileOpen: boolean;
  onCloseMobile(): void;
  onHome(): void;
  onSelect(id: string): void;
  onLogout(): void;
}

export function Sidebar({ modules, selectedSectionId, view, mobileOpen, onCloseMobile, onHome, onSelect, onLogout }: SidebarProps) {
  const main = modules.filter((module) => module.group === "main");
  const system = modules.filter((module) => module.group === "system");
  const select = (id: string) => { onSelect(id); onCloseMobile(); };
  const goHome = () => { onHome(); onCloseMobile(); };
  return <>
    <button className="osk-sidebar-backdrop" data-open={mobileOpen || undefined} onClick={onCloseMobile} aria-label="Fechar menu" />
    <aside className="osk-dashboard-sidebar" data-open={mobileOpen || undefined}>
      <div className="osk-sidebar-brand"><Brand /><button className="osk-sidebar-close" onClick={onCloseMobile} aria-label="Fechar menu"><X size={18} /></button></div>
      <nav>
        <button className="osk-sidebar-link" data-active={view === "home" || undefined} onClick={goHome}><Home size={17} /><span>Visão geral</span></button>
        <span className="osk-sidebar-label">Módulos</span>
        {main.map((module) => <SidebarLink key={module.id} module={module} active={view === "section" && selectedSectionId === module.id} onClick={() => select(module.id)} />)}
        {system.length > 0 && <span className="osk-sidebar-label">Sistema</span>}
        {system.map((module) => <SidebarLink key={module.id} module={module} active={view === "section" && selectedSectionId === module.id} onClick={() => select(module.id)} />)}
      </nav>
      <button className="osk-sidebar-logout" onClick={onLogout}><LogOut size={16} /> Encerrar sessão</button>
    </aside>
  </>;
}

function SidebarLink({ module, active, onClick }: { module: DashboardVisualModule; active: boolean; onClick(): void }) {
  const Icon = module.icon;
  return <button className="osk-sidebar-link" data-active={active || undefined} onClick={onClick}><Icon size={17} /><span>{module.label}</span><i data-enabled={module.enabled !== false || undefined} /></button>;
}
