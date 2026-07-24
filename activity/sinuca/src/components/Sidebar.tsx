import { Home, LogOut, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type TransitionEvent,
} from "react";
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

const DRAWER_TRANSITION_MS = 280;

type DrawerPointer = {
  pointerId: number;
  startX: number;
  startY: number;
  latestX: number;
  latestAt: number;
  velocityX: number;
  horizontal: boolean;
};

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
  const asideRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const pointerRef = useRef<DrawerPointer | null>(null);
  const suppressClickRef = useRef(false);
  const [mobileMounted, setMobileMounted] = useState(mobileOpen);
  const [mobileVisible, setMobileVisible] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState(0);

  useEffect(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }

    let firstFrame = 0;
    let secondFrame = 0;
    if (mobileOpen) {
      setMobileMounted(true);
      setDragOffset(0);
      firstFrame = window.requestAnimationFrame(() => {
        secondFrame = window.requestAnimationFrame(() => setMobileVisible(true));
      });
    } else {
      setDragging(false);
      setMobileVisible(false);
      closeTimerRef.current = window.setTimeout(() => {
        setMobileMounted(false);
        setDragOffset(0);
        closeTimerRef.current = null;
      }, DRAWER_TRANSITION_MS + 80);
    }

    return () => {
      window.cancelAnimationFrame(firstFrame);
      window.cancelAnimationFrame(secondFrame);
    };
  }, [mobileOpen]);

  useEffect(() => {
    if (!mobileMounted) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = mobileVisible ? window.setTimeout(() => closeRef.current?.focus(), 90) : 0;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseMobile();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      if (focusTimer) window.clearTimeout(focusTimer);
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
    setDragOffset(0);
  };

  const beginCloseSwipe = (event: ReactPointerEvent<HTMLElement>) => {
    if (!mobileOpen || window.innerWidth > 980 || !event.isPrimary || event.pointerType === "mouse") return;
    pointerRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      latestX: event.clientX,
      latestAt: event.timeStamp,
      velocityX: 0,
      horizontal: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const moveCloseSwipe = (event: ReactPointerEvent<HTMLElement>) => {
    const pointer = pointerRef.current;
    if (!pointer || pointer.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - pointer.startX;
    const deltaY = event.clientY - pointer.startY;

    if (!pointer.horizontal) {
      if (Math.abs(deltaY) > 14 && Math.abs(deltaY) > Math.abs(deltaX) * 1.15) {
        pointerRef.current = null;
        return;
      }
      if (deltaX <= -9 && Math.abs(deltaX) > Math.abs(deltaY) * 1.15) {
        pointer.horizontal = true;
        setDragging(true);
      }
    }
    if (!pointer.horizontal) return;

    event.preventDefault();
    const elapsed = Math.max(1, event.timeStamp - pointer.latestAt);
    pointer.velocityX = (event.clientX - pointer.latestX) / elapsed;
    pointer.latestX = event.clientX;
    pointer.latestAt = event.timeStamp;
    const width = asideRef.current?.getBoundingClientRect().width || 280;
    setDragOffset(Math.max(-width, Math.min(0, deltaX)));
  };

  const finishCloseSwipe = (event: ReactPointerEvent<HTMLElement>) => {
    const pointer = pointerRef.current;
    if (!pointer || pointer.pointerId !== event.pointerId) return;
    pointerRef.current = null;
    if (!pointer.horizontal) return;

    suppressClickRef.current = true;
    window.setTimeout(() => { suppressClickRef.current = false; }, 80);
    const width = asideRef.current?.getBoundingClientRect().width || 280;
    const finalOffset = Math.max(-width, Math.min(0, event.clientX - pointer.startX));
    const shouldClose = finalOffset <= -Math.min(88, width * .26) || pointer.velocityX <= -.48;
    setDragging(false);
    if (shouldClose) onCloseMobile();
    else setDragOffset(0);
  };

  const cancelCloseSwipe = () => {
    pointerRef.current = null;
    setDragging(false);
    setDragOffset(0);
  };

  const preventClickAfterDrag = (event: ReactMouseEvent<HTMLElement>) => {
    if (!suppressClickRef.current) return;
    event.preventDefault();
    event.stopPropagation();
  };

  const select = (id: string) => onSelect(id);
  const goHome = () => onHome();
  const drawerWidth = asideRef.current?.getBoundingClientRect().width || 280;
  const dragProgress = dragging ? Math.max(0, 1 - Math.abs(dragOffset) / drawerWidth) : 1;

  return <>
    {mobileMounted && <button
      type="button"
      className="osk-sidebar-backdrop"
      data-open={mobileVisible || undefined}
      style={{ "--osk-drawer-progress": dragProgress } as CSSProperties}
      onClick={onCloseMobile}
      aria-label="Fechar menu"
      tabIndex={mobileVisible ? 0 : -1}
    />}
    <aside
      ref={asideRef}
      className="osk-dashboard-sidebar"
      data-open={mobileVisible || undefined}
      data-mobile-mounted={mobileMounted || undefined}
      data-dragging={dragging || undefined}
      style={{ "--osk-drawer-drag-x": `${dragOffset}px` } as CSSProperties}
      aria-label="Navegação do painel"
      onTransitionEnd={finishClose}
      onPointerDown={beginCloseSwipe}
      onPointerMove={moveCloseSwipe}
      onPointerUp={finishCloseSwipe}
      onPointerCancel={cancelCloseSwipe}
      onClickCapture={preventClickAfterDrag}
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
