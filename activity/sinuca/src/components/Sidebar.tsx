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
      setMobileVisible(false);
      setDragging(false);
      setDragOffset(0);

      firstFrame = window.requestAnimationFrame(() => {
        // Força o estado inicial fora da tela a ser calculado antes da abertura.
        void asideRef.current?.getBoundingClientRect().width;
        secondFrame = window.requestAnimationFrame(() => setMobileVisible(true));
      });
    } else if (mobileMounted) {
      setDragging(false);
      setMobileVisible(false);
      closeTimerRef.current = window.setTimeout(() => {
        setMobileMounted(false);
        setDragOffset(0);
        closeTimerRef.current = null;
      }, DRAWER_TRANSITION_MS + 90);
    }

    return () => {
      window.cancelAnimationFrame(firstFrame);
      window.cancelAnimationFrame(secondFrame);
    };
  }, [mobileMounted, mobileOpen]);

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
    asideRef.current?.setPointerCapture?.(event.pointerId);
  };

  useEffect(() => {
    const resetGesture = (pointerId?: number) => {
      pointerRef.current = null;
      if (pointerId !== undefined && asideRef.current?.hasPointerCapture?.(pointerId)) {
        asideRef.current.releasePointerCapture?.(pointerId);
      }
      setDragging(false);
      setDragOffset(0);
    };

    const onPointerMove = (event: PointerEvent) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId || !mobileOpen) return;
      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;

      if (!pointer.horizontal) {
        if (Math.abs(deltaY) > 14 && Math.abs(deltaY) > Math.abs(deltaX) * 1.12) {
          resetGesture(event.pointerId);
          return;
        }
        if (deltaX <= -7 && Math.abs(deltaX) > Math.abs(deltaY) * 1.12) {
          pointer.horizontal = true;
          suppressClickRef.current = true;
          setDragging(true);
        }
      }
      if (!pointer.horizontal) return;

      if (event.cancelable) event.preventDefault();
      const elapsed = Math.max(1, event.timeStamp - pointer.latestAt);
      pointer.velocityX = (event.clientX - pointer.latestX) / elapsed;
      pointer.latestX = event.clientX;
      pointer.latestAt = event.timeStamp;
      const width = asideRef.current?.getBoundingClientRect().width || 280;
      setDragOffset(Math.max(-width, Math.min(0, deltaX)));
    };

    const finishGesture = (event: PointerEvent, cancelled = false) => {
      const pointer = pointerRef.current;
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      pointerRef.current = null;

      if (asideRef.current?.hasPointerCapture?.(event.pointerId)) {
        asideRef.current.releasePointerCapture?.(event.pointerId);
      }

      const deltaX = event.clientX - pointer.startX;
      const deltaY = event.clientY - pointer.startY;
      const horizontalSwipe = pointer.horizontal
        || (deltaX <= -42 && Math.abs(deltaX) > Math.abs(deltaY) * 1.12);
      if (!horizontalSwipe) {
        setDragging(false);
        setDragOffset(0);
        return;
      }

      suppressClickRef.current = true;
      window.setTimeout(() => { suppressClickRef.current = false; }, 120);
      if (cancelled) {
        setDragging(false);
        setDragOffset(0);
        return;
      }

      const width = asideRef.current?.getBoundingClientRect().width || 280;
      const finalOffset = Math.max(-width, Math.min(0, deltaX));
      const shouldClose = finalOffset <= -Math.min(72, width * .22) || pointer.velocityX <= -.42;
      setDragging(false);

      if (shouldClose) {
        setDragOffset(finalOffset);
        onCloseMobile();
      } else {
        setDragOffset(0);
      }
    };

    const onPointerUp = (event: PointerEvent) => finishGesture(event);
    const onPointerCancel = (event: PointerEvent) => finishGesture(event, true);

    window.addEventListener("pointermove", onPointerMove, { capture: true, passive: false });
    window.addEventListener("pointerup", onPointerUp, { capture: true, passive: true });
    window.addEventListener("pointercancel", onPointerCancel, { capture: true, passive: true });
    return () => {
      window.removeEventListener("pointermove", onPointerMove, true);
      window.removeEventListener("pointerup", onPointerUp, true);
      window.removeEventListener("pointercancel", onPointerCancel, true);
    };
  }, [mobileOpen, onCloseMobile]);

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
      onPointerDownCapture={beginCloseSwipe}
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
