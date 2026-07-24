import { Home, LogOut, X } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties, type TouchEvent as ReactTouchEvent, type TransitionEvent } from "react";
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

const DRAWER_TRANSITION_MS = 260;

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
  const closeTimerRef = useRef<number | null>(null);
  const closeSwipeRef = useRef<{ x: number; y: number } | null>(null);
  const [mobileMounted, setMobileMounted] = useState(mobileOpen);
  const [mobileVisible, setMobileVisible] = useState(false);

  useEffect(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }

    let firstFrame = 0;
    let secondFrame = 0;
    if (mobileOpen) {
      setMobileMounted(true);
      firstFrame = window.requestAnimationFrame(() => {
        secondFrame = window.requestAnimationFrame(() => setMobileVisible(true));
      });
    } else {
      setMobileVisible(false);
      closeTimerRef.current = window.setTimeout(() => {
        setMobileMounted(false);
        closeTimerRef.current = null;
      }, DRAWER_TRANSITION_MS + 60);
    }

    return () => {
      window.cancelAnimationFrame(firstFrame);
      window.cancelAnimationFrame(secondFrame);
    };
  }, [mobileOpen]);

  useEffect(() => {
    if (!mobileMounted || !mobileVisible) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 80);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseMobile();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [mobileMounted, mobileVisible, onCloseMobile]);

  useEffect(() => () => {
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, []);

  const finishClose = (event: TransitionEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget || event.propertyName !== "transform" || mobileOpen) return;
    setMobileMounted(false);
  };

  const beginCloseSwipe = (event: ReactTouchEvent<HTMLElement>) => {
    if (!mobileOpen || event.touches.length !== 1) return;
    closeSwipeRef.current = { x: event.touches[0].clientX, y: event.touches[0].clientY };
  };

  const finishCloseSwipe = (event: ReactTouchEvent<HTMLElement>) => {
    const start = closeSwipeRef.current;
    closeSwipeRef.current = null;
    if (!start || event.changedTouches.length !== 1) return;
    const deltaX = event.changedTouches[0].clientX - start.x;
    const deltaY = event.changedTouches[0].clientY - start.y;
    if (deltaX <= -58 && Math.abs(deltaY) <= Math.max(44, Math.abs(deltaX) * .62)) onCloseMobile();
  };

  const select = (id: string) => onSelect(id);
  const goHome = () => onHome();

  return <>
    {mobileMounted && <button
      type="button"
      className="osk-sidebar-backdrop"
      data-open={mobileVisible || undefined}
      onClick={onCloseMobile}
      aria-label="Fechar menu"
      tabIndex={mobileVisible ? 0 : -1}
    />}
    <aside
      className="osk-dashboard-sidebar"
      data-open={mobileVisible || undefined}
      data-mobile-mounted={mobileMounted || undefined}
      aria-label="Navegação do painel"
      onTransitionEnd={finishClose}
      onTouchStart={beginCloseSwipe}
      onTouchEnd={finishCloseSwipe}
      onTouchCancel={() => { closeSwipeRef.current = null; }}
    >
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
  return <button className="osk-sidebar-link" style={{ "--osk-menu-index": index } as CSSProperties} data-active={active || undefined} onClick={onClick}>
    <Icon size={18} />
    <span>{item.label}</span>
  </button>;
}
