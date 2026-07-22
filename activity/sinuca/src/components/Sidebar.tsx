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
  const main = modules.filter((item) => item.group === "main");
  const system = modules.filter((item) => item.group === "system");
  const select = (id: string) => { onSelect(id); onCloseMobile(); };
  const goHome = () => { onHome(); onCloseMobile(); };

  return <>
    <button className="osk-sidebar-backdrop" data-open={mobileOpen || undefined} onClick={onCloseMobile} aria-label="Fechar menu" />
    <aside className="osk-dashboard-sidebar" data-open={mobileOpen || undefined}>
      <div className="osk-sidebar-brand"><Brand /><button className="osk-sidebar-close" onClick={onCloseMobile} aria-label="Fechar menu"><X size={18} /></button></div>
      <nav>
        <button className="osk-sidebar-link" data-active={view === "home" || undefined} onClick={goHome}><Home size={17} /><span>Início</span></button>
        <span className="osk-sidebar-label">Funções</span>
        {main.map((item) => <SidebarLink key={item.id} item={item} active={view === "section" && selectedSectionId === item.id} onClick={() => select(item.id)} />)}
        {system.length > 0 && <span className="osk-sidebar-label">Configurações</span>}
        {system.map((item) => <SidebarLink key={item.id} item={item} active={view === "section" && selectedSectionId === item.id} onClick={() => select(item.id)} />)}
      </nav>
      <button className="osk-sidebar-logout" onClick={onLogout}><LogOut size={16} /> Sair do painel</button>
    </aside>
  </>;
}

function SidebarLink({ item, active, onClick }: { item: DashboardVisualModule; active: boolean; onClick(): void }) {
  const Icon = item.icon;
  return <button className="osk-sidebar-link" data-active={active || undefined} onClick={onClick}><Icon size={17} /><span>{item.label}</span></button>;
}
