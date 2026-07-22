import { Menu, RefreshCw, Server } from "lucide-react";
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
  return <header className="osk-dashboard-topbar">
    <div className="osk-topbar-server">
      <button className="osk-topbar-menu" onClick={onOpenMenu} aria-label="Abrir menu"><Menu size={21} /></button>
      <SmartAvatar className="osk-topbar-server-avatar" src={guildIcon} name={guildName} type="server" alt={guildName} size={38} />
      <span><strong>{guildName}</strong><small>Configurações da Osaka</small></span>
    </div>
    <div className="osk-topbar-actions">
      <button onClick={onRefresh} disabled={busy} title="Atualizar dados" aria-label="Atualizar dados"><RefreshCw size={17} className={busy ? "osk-spin" : undefined} /><span>Atualizar</span></button>
      <button onClick={onChangeServer} title="Trocar servidor" aria-label="Trocar servidor"><Server size={17} /><span>Servidores</span></button>
    </div>
  </header>;
}
