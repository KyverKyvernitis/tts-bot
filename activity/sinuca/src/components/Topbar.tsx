import { Menu, MoreHorizontal, RefreshCw, Server } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { SmartAvatar } from "./SmartAvatar";

interface TopbarProps {
  guildName: string;
  guildIcon?: string | null;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer(): void;
  onOpenMenu(): void;
}

export function Topbar({ guildName, guildIcon, busy, onRefresh, onChangeServer, onOpenMenu }: TopbarProps) {
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!actionsOpen) return;
    const close = (event: MouseEvent | TouchEvent) => {
      if (actionsRef.current && !actionsRef.current.contains(event.target as Node)) setActionsOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setActionsOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [actionsOpen]);

  return <header className="osk-dashboard-topbar">
    <div className="osk-topbar-server">
      <button className="osk-topbar-menu" onClick={onOpenMenu} aria-label="Abrir menu"><Menu size={22} /></button>
      <SmartAvatar className="osk-topbar-server-avatar" src={guildIcon} name={guildName} type="server" alt={guildName} size={38} />
      <span><strong>{guildName}</strong><small>Configurações da Osaka</small></span>
    </div>
    <div className="osk-topbar-actions" ref={actionsRef}>
      <button className="osk-topbar-more" onClick={() => setActionsOpen((current) => !current)} aria-label="Ações do servidor" aria-expanded={actionsOpen}><MoreHorizontal size={19} /></button>
      <div className="osk-topbar-popover" data-open={actionsOpen || undefined}>
        <button onClick={() => { setActionsOpen(false); onRefresh(); }} disabled={busy}><RefreshCw size={17} className={busy ? "osk-spin" : undefined} /><span>{busy ? "Atualizando..." : "Atualizar dados"}</span></button>
        <button onClick={() => { setActionsOpen(false); onChangeServer(); }}><Server size={17} /><span>Trocar servidor</span></button>
      </div>
    </div>
  </header>;
}
