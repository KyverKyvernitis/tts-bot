import { RefreshCw, Server } from "lucide-react";
import { SmartAvatar } from "./SmartAvatar";

interface TopbarProps {
  guildName: string;
  guildIcon?: string | null;
  runtime: "activity" | "browser";
  userName: string;
  userAvatar?: string | null;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer?(): void;
}

export function Topbar({ guildName, guildIcon, runtime, userName, userAvatar, busy, onRefresh, onChangeServer }: TopbarProps) {
  return (
    <header className="osk-topbar">
      <div className="osk-guild">
        <SmartAvatar className="osk-guild-avatar" src={guildIcon} name={guildName} type="server" alt={guildName} />
        <div className="osk-guild-text">
          <strong>{guildName}</strong>
          <small>{runtime === "browser" ? "Dashboard web" : "Discord Activity"}</small>
        </div>
      </div>

      <div className="osk-top-actions">
        <button className="osk-icon-btn" onClick={onRefresh} aria-label="Atualizar" disabled={busy}>
          <RefreshCw size={16} className={busy ? "osk-spin" : undefined} />
        </button>
        {runtime === "browser" && onChangeServer && (
          <button className="osk-icon-btn" onClick={onChangeServer} aria-label="Trocar servidor">
            <Server size={16} />
          </button>
        )}
        <div className="osk-user-chip">
          <SmartAvatar className="osk-user-chip-avatar" src={userAvatar} name={userName} type="user" alt={userName} />
          <small>{userName}</small>
        </div>
      </div>
    </header>
  );
}
