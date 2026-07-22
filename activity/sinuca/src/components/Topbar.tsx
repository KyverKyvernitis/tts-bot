import { Menu, RefreshCw, Server, UserRound } from "lucide-react";
import { SmartAvatar } from "./SmartAvatar";

interface TopbarProps {
  guildName: string;
  guildIcon?: string | null;
  userName: string;
  userAvatar?: string | null;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer(): void;
  onOpenMenu(): void;
}

export function Topbar({ guildName, guildIcon, userName, userAvatar, busy, onRefresh, onChangeServer, onOpenMenu }: TopbarProps) {
  return <header className="osk-dashboard-topbar">
    <div className="osk-topbar-server">
      <button className="osk-topbar-menu" onClick={onOpenMenu} aria-label="Abrir menu"><Menu size={20} /></button>
      <SmartAvatar src={guildIcon} name={guildName} type="server" alt={guildName} size={38} />
      <span><strong>{guildName}</strong><small>Painel do servidor</small></span>
    </div>
    <div className="osk-topbar-actions">
      <button onClick={onRefresh} disabled={busy} title="Atualizar dados"><RefreshCw size={16} className={busy ? "osk-spin" : undefined} /><span>Atualizar</span></button>
      <button onClick={onChangeServer} title="Trocar servidor"><Server size={16} /><span>Trocar</span></button>
      <div className="osk-topbar-user"><SmartAvatar src={userAvatar} name={userName} type="user" alt={userName} size={30} /><span>{userName}</span><UserRound size={14} /></div>
    </div>
  </header>;
}
