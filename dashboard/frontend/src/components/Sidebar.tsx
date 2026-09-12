import { Command, LayoutGrid, LogOut, Settings2, X } from "lucide-react";
import type { CSSProperties } from "react";
import { SmartAvatar } from "./SmartAvatar";
import { useSidebarDrawer } from "./sidebar/useSidebarDrawer";

export type DashboardNavigationPage = "general" | "modules" | "commands";

interface SidebarProps {
  activePage: DashboardNavigationPage;
  mobileOpen: boolean;
  botName?: string;
  botAvatarUrl?: string | null;
  gestureDisabled?: boolean;
  onCloseMobile(): void;
  onOpenMobile(): void;
  onNavigate(page: DashboardNavigationPage): void;
  onLogout(): void;
}

export function Sidebar({
  activePage,
  mobileOpen,
  botName = "Osaka",
  botAvatarUrl,
  gestureDisabled = false,
  onCloseMobile,
  onOpenMobile,
  onNavigate,
  onLogout,
}: SidebarProps) {
  const {
    asideRef,
    closeRef,
    visualOpen,
    dragging,
    dragOffset,
    progress,
    close,
  } = useSidebarDrawer({ mobileOpen, gestureDisabled, onCloseMobile, onOpenMobile });

  return <>
    <button
      type="button"
      className="osk-sidebar-backdrop"
      data-open={visualOpen || undefined}
      data-dragging={dragging || undefined}
      style={{ "--osk-drawer-progress": progress } as CSSProperties}
      onClick={close}
      aria-label="Fechar menu"
      tabIndex={visualOpen ? 0 : -1}
    />
    <aside
      ref={asideRef}
      className="osk-dashboard-sidebar"
      data-open={visualOpen || undefined}
      data-dragging={dragging || undefined}
      style={{ "--osk-drawer-drag-x": `${dragOffset}px` } as CSSProperties}
      aria-label="Navegação do painel"
    >
      <div className="osk-sidebar-brand">
        <span className="osk-sidebar-bot">
          <span className="osk-sidebar-bot-glow" aria-hidden="true" />
          <SmartAvatar className="osk-sidebar-bot-avatar" src={botAvatarUrl} name={botName} type="user" alt={`Avatar da ${botName}`} size={54} />
          <span className="osk-sidebar-bot-copy"><strong>{botName}</strong><small>Painel do bot</small></span>
        </span>
        <button ref={closeRef} type="button" className="osk-sidebar-close" onClick={close} aria-label="Fechar menu"><X size={21} /></button>
      </div>
      <nav>
        <SidebarLink label="Geral" icon={Settings2} index={0} active={activePage === "general"} onClick={() => onNavigate("general")} />
        <SidebarLink label="Módulos" icon={LayoutGrid} index={1} active={activePage === "modules"} onClick={() => onNavigate("modules")} />
        <SidebarLink label="Comandos" icon={Command} index={2} active={activePage === "commands"} onClick={() => onNavigate("commands")} />
      </nav>
      <button className="osk-sidebar-logout" onClick={onLogout}><LogOut size={17} /> Sair do painel</button>
    </aside>
  </>;
}

function SidebarLink({ label, icon: Icon, active, onClick, index }: { label: string; icon: typeof Settings2; active: boolean; onClick(): void; index: number }) {
  return <button className="osk-sidebar-link" style={{ "--osk-menu-index": index } as CSSProperties} data-active={active || undefined} aria-current={active ? "page" : undefined} onClick={onClick}>
    <Icon size={18} />
    <span>{label}</span>
  </button>;
}
