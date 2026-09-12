import { Menu } from "lucide-react";
import type {
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { SmartAvatar } from "./SmartAvatar";

interface TopbarProps {
  guildName: string;
  guildIcon?: string | null;
  user: DashboardUserPayload;
  supportServer: DashboardSupportServerPayload | null;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer(): void;
  onLogout(): void;
  onOpenMenu(): void;
}

export function Topbar({
  guildName,
  guildIcon,
  user,
  supportServer,
  busy,
  onRefresh,
  onChangeServer,
  onLogout,
  onOpenMenu,
}: TopbarProps) {
  return <header className="osk-dashboard-topbar">
    <div className="osk-topbar-server">
      <button className="osk-topbar-menu" onClick={onOpenMenu} aria-label="Abrir menu"><Menu size={22} /></button>
      <SmartAvatar className="osk-topbar-server-avatar" src={guildIcon} name={guildName} type="server" alt={guildName} size={38} />
      <span><strong>{guildName}</strong><small>Configurações da Osaka</small></span>
    </div>
    <AccountMenu
      user={user}
      currentServer={{ name: guildName, icon: guildIcon || null }}
      busy={busy}
      supportInviteUrl={supportServer?.inviteUrl}
      onServers={onChangeServer}
      onRefresh={onRefresh}
      onLogout={onLogout}
    />
  </header>;
}
