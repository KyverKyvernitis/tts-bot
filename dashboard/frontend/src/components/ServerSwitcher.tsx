import { useMemo } from "react";
import type { DashboardServerCard } from "../types/dashboard";
import { SmartAvatar } from "./SmartAvatar";
import { SmartSelect } from "./SmartSelect";

interface ServerSwitcherProps {
  guildId: string;
  guildName: string;
  guildIcon?: string | null;
  servers: DashboardServerCard[];
  onSelect(guildId: string): void;
}

export function ServerSwitcher({ guildId, guildName, guildIcon, servers, onSelect }: ServerSwitcherProps) {
  const choices = useMemo(() => servers.filter((server) => server.canManage && server.botPresent), [servers]);
  const options = useMemo(() => choices.map((server) => ({ value: server.id, label: server.name })), [choices]);
  const avatar = (id: string, name: string) => <SmartAvatar src={choices.find((server) => server.id === id)?.icon || (id === guildId ? guildIcon : null)} name={name} type="server" size={34} />;

  if (choices.length < 2) return <div className="osk-server-identity">
    {avatar(guildId, guildName)}<span><small>Servidor</small><strong>{guildName}</strong></span>
  </div>;

  return <div className="osk-server-switcher"><SmartSelect
    id="dashboard-server"
    ariaLabel="Escolher servidor"
    caption="Servidor"
    menuTitle="Seus servidores"
    presentation="anchored"
    value={guildId}
    options={options}
    placeholder={guildName}
    renderLeading={(option) => avatar(option.value, option.label)}
    onChange={onSelect}
  /></div>;
}
