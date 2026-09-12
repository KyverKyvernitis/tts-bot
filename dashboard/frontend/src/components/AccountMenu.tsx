import {
  ChevronDown,
  ChevronRight,
  LogOut,
  MessagesSquare,
  RefreshCw,
  Server,
} from "lucide-react";
import { type CSSProperties } from "react";
import { createPortal } from "react-dom";
import type { DashboardServerCard, DashboardUserPayload } from "../types/dashboard";
import { accountIdentityName } from "./account-menu/accountMenuModel";
import { useAccountMenu } from "./account-menu/useAccountMenu";
import { SmartAvatar } from "./SmartAvatar";

interface AccountMenuProps {
  user: DashboardUserPayload;
  currentServer?: Pick<DashboardServerCard, "name" | "icon"> | null;
  busy?: boolean;
  variant?: "landing" | "header";
  serversLabel?: string;
  showServersAction?: boolean;
  supportInviteUrl?: string;
  onServers(): void;
  onRefresh(): void;
  onLogout(): void;
}

export function AccountMenu({
  user,
  currentServer = null,
  busy = false,
  variant = "header",
  serversLabel = currentServer ? "Trocar servidor" : "Meus servidores",
  showServersAction = true,
  supportInviteUrl = "https://discord.gg/RckuzJbvVk",
  onServers,
  onRefresh,
  onLogout,
}: AccountMenuProps) {
  const menu = useAccountMenu();
  const name = accountIdentityName(user);

  return <>
    <button
      ref={menu.triggerRef}
      type="button"
      className="osk-account-trigger"
      data-variant={variant}
      onClick={menu.toggle}
      aria-label={`${menu.visible ? "Fechar" : "Abrir"} menu da conta de ${name}`}
      aria-controls={menu.menuId}
      aria-expanded={menu.visible}
      aria-haspopup="menu"
    >
      <SmartAvatar
        className="osk-account-trigger-avatar"
        src={user.avatarUrl}
        name={name}
        type="user"
        alt={`Avatar de ${name}`}
        size={variant === "landing" ? 32 : 30}
      />
      <span className="osk-account-trigger-copy">
        <small>Conta</small>
        <strong>{name}</strong>
      </span>
      <ChevronDown size={15} aria-hidden="true" />
    </button>

    {menu.mounted && createPortal(
      <div className="osk-account-layer" data-visible={menu.visible || undefined}>
        <button type="button" className="osk-account-backdrop" onClick={() => menu.close()} aria-label="Fechar menu da conta" />
        <section
          id={menu.menuId}
          ref={menu.sheetRef}
          className="osk-account-sheet"
          style={{
            "--osk-account-top": `${menu.position.top}px`,
            "--osk-account-right": `${menu.position.right}px`,
            "--osk-account-max-height": `${menu.position.maxHeight}px`,
          } as CSSProperties}
          role="menu"
          aria-label="Menu da conta"
          aria-hidden={!menu.visible}
        >
          <header className="osk-account-profile">
            <SmartAvatar className="osk-account-profile-avatar" src={user.avatarUrl} name={name} type="user" alt="" size={38} />
            <span>
              <strong>{name}</strong>
              {user.username && <small>@{user.username}</small>}
            </span>
          </header>

          {currentServer && <div className="osk-account-current-server">
            <SmartAvatar src={currentServer.icon} name={currentServer.name} type="server" alt="" size={30} />
            <span><small>Configurando</small><strong>{currentServer.name}</strong></span>
          </div>}

          <nav className="osk-account-actions">
            {showServersAction && <button type="button" role="menuitem" onClick={() => menu.run(onServers, false)}>
              <Server size={17} /><span>{serversLabel}</span><ChevronRight size={15} />
            </button>}
            <button type="button" role="menuitem" onClick={() => menu.run(onRefresh)} disabled={busy}>
              <RefreshCw size={17} className={busy ? "osk-spin" : undefined} /><span>{busy ? "Atualizando..." : "Atualizar dados"}</span>
            </button>
            <a role="menuitem" href={supportInviteUrl} target="_blank" rel="noreferrer noopener" onClick={() => menu.close(false)}>
              <MessagesSquare size={17} /><span>Servidor de suporte</span><ChevronRight size={15} />
            </a>
            <button type="button" role="menuitem" className="osk-account-logout" onClick={() => menu.run(onLogout, false)}>
              <LogOut size={17} /><span>Sair</span>
            </button>
          </nav>
        </section>
      </div>,
      document.body,
    )}
  </>;
}
