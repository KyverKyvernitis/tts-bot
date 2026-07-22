import { Home, LogOut, X } from "lucide-react";
import { useEffect, useRef, type CSSProperties } from "react";
import type { DashboardVisualModule } from "../moduleCatalog";
import { SmartAvatar } from "./SmartAvatar";

interface SidebarProps {
  modules: DashboardVisualModule[];
  selectedSectionId: string;
  view: "home" | "section";
  mobileOpen: boolean;
  botName?: string;
  botAvatarUrl?: string | null;
  onCloseMobile(): void;
  onHome(): void;
  onSelect(id: string): void;
  onLogout(): void;
}

export function Sidebar({
  modules,
  selectedSectionId,
  view,
  mobileOpen,
  botName = "Osaka",
  botAvatarUrl,
  onCloseMobile,
  onHome,
  onSelect,
  onLogout,
}: SidebarProps) {
  const main = modules.filter((item) => item.group === "main");
  const system = modules.filter((item) => item.group === "system");
  const closeRef = useRef<HTMLButtonElement>(null);
  const select = (id: string) => { onSelect(id); onCloseMobile(); };
  const goHome = () => { onHome(); onCloseMobile(); };

  useEffect(() => {
    if (!mobileOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 80);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseMobile();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [mobileOpen, onCloseMobile]);

  return <>
    <button
      type="button"
      className="osk-sidebar-backdrop"
      data-open={mobileOpen || undefined}
      onClick={onCloseMobile}
      aria-label="Fechar menu"
      tabIndex={mobileOpen ? 0 : -1}
    />
    <aside className="osk-dashboard-sidebar" data-open={mobileOpen || undefined} aria-label="Navegação do painel">
      <div className="osk-sidebar-brand">
        <span className="osk-sidebar-bot">
          <span className="osk-sidebar-bot-glow" aria-hidden="true" />
          <SmartAvatar className="osk-sidebar-bot-avatar" src={botAvatarUrl} name={botName} type="user" alt={`Avatar da ${botName}`} size={54} />
          <span className="osk-sidebar-bot-copy"><strong>{botName}</strong><small>Painel do bot</small></span>
        </span>
        <button ref={closeRef} type="button" className="osk-sidebar-close" onClick={onCloseMobile} aria-label="Fechar menu"><X size={21} /></button>
      </div>
      <nav>
        <button className="osk-sidebar-link" data-active={view === "home" || undefined} onClick={goHome}><Home size={18} /><span>Início</span></button>
        <span className="osk-sidebar-label">Funções</span>
        {main.map((item, index) => <SidebarLink key={item.id} item={item} index={index} active={view === "section" && selectedSectionId === item.id} onClick={() => select(item.id)} />)}
        {system.length > 0 && <span className="osk-sidebar-label">Configurações</span>}
        {system.map((item, index) => <SidebarLink key={item.id} item={item} index={main.length + index} active={view === "section" && selectedSectionId === item.id} onClick={() => select(item.id)} />)}
      </nav>
      <button className="osk-sidebar-logout" onClick={onLogout}><LogOut size={17} /> Sair do painel</button>
    </aside>
  </>;
}

function SidebarLink({ item, active, onClick, index }: { item: DashboardVisualModule; active: boolean; onClick(): void; index: number }) {
  const Icon = item.icon;
  return <button className="osk-sidebar-link" style={{ "--osk-menu-index": index } as CSSProperties} data-active={active || undefined} onClick={onClick}><Icon size={18} /><span>{item.label}</span></button>;
}
