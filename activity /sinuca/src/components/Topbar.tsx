import { RefreshCw, Server } from "lucide-react";
import { guildInitials } from "../moduleCatalog";

interface TopbarProps {
  guildName: string;
  runtime: "activity" | "browser";
  userName: string;
  busy?: boolean;
  onRefresh(): void;
  onChangeServer?(): void;
}

export function Topbar({ guildName, runtime, userName, busy, onRefresh, onChangeServer }: TopbarProps) {
  return (
    <header className="osk-topbar">
      <div className="osk-guild">
        <div className="osk-guild-avatar">{guildInitials(guildName)}</div>
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
          <span className="osk-user-chip-avatar">{guildInitials(userName)}</span>
          <small>{userName}</small>
        </div>
      </div>
    </header>
  );
}
