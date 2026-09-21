import { Menu } from "lucide-react";
import type {
  DashboardServerCard,
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { SmartAvatar } from "./SmartAvatar";
import { ServerSwitcher } from "./ServerSwitcher";

interface TopbarProps {
  guildId: string;
  guildName: string;
  guildIcon?: string | null;
  servers: DashboardServerCard[];
  botName: string;
  botAvatarUrl?: string | null;
  user: DashboardUserPayload;
  supportServer: DashboardSupportServerPayload | null;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer(): void;
  onSelectServer(guildId: string): void;
  onLogout(): void;
  onOpenMenu(): void;
}

export function Topbar({
  guildId,
  guildName,
  guildIcon,
  servers,
  botName,
  botAvatarUrl,
  user,
  supportServer,
  busy,
  onRefresh,
  onChangeServer,
  onSelectServer,
  onLogout,
  onOpenMenu,
}: TopbarProps) {
  return <header className="osk-dashboard-topbar">
    <button type="button" className="osk-topbar-menu" onClick={onOpenMenu} aria-label="Abrir menu"><Menu size={22} /></button>
    <span className="osk-topbar-brand"><SmartAvatar src={botAvatarUrl} name={botName} type="user" size={34} /><strong>{botName}</strong></span>
    <ServerSwitcher guildId={guildId} guildName={guildName} guildIcon={guildIcon} servers={servers} onSelect={onSelectServer} />
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
