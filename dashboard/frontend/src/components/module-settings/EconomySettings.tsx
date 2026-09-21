import { Hash, MessageSquare, ShieldCheck, Spade } from "lucide-react";
import type { DashboardFieldDefinition, DashboardOptionsPayload } from "../../types/dashboard";
import { DashboardFieldControl } from "../DashboardFieldControl";
import { ModuleArtwork } from "../ModuleArtwork";

interface Props { fields: DashboardFieldDefinition[]; draft: Record<string, unknown>; guildOptions: DashboardOptionsPayload | null; onChange(field: DashboardFieldDefinition, value: unknown): void }
const settings = [
  { id: "economy.channel_id", title: "Onde jogar", icon: Hash, note: "Escolha o canal das partidas ou permita todos os canais compatíveis." },
  { id: "economy.input_mode", title: "Como iniciar", icon: MessageSquare, note: "Defina como os membros iniciam os jogos com fichas." },
  { id: "economy.staff_role_id", title: "Equipe da economia", icon: ShieldCheck, note: "Cargo da equipe responsável pelos jogos e pelas fichas." },
];
export function EconomySettings({ fields, draft, guildOptions, onChange }: Props) {
  const channelId = String(draft["economy.channel_id"] || "");
  const channel = guildOptions?.channels.find(item => item.id === channelId);
  const location = !channelId || channelId === "0" ? "Todos os canais compatíveis" : channel ? `#${channel.name}` : "Canal configurado";
  const mode = draft["economy.input_mode"] === "commands" ? "Comandos com prefixo" : "Palavras no chat e comandos";
  return <div className="osk-economy-settings">
    <div className="osk-economy-table"><ModuleArtwork moduleId="economy" fallbackIcon={Spade} /><div><span className="osk-module-eyebrow">Jogos com fichas</span><h2>A mesa do seu servidor</h2><p>{location}<span aria-hidden="true"> · </span>{mode}</p></div></div>
    <div className="osk-economy-controls">{settings.map(({ id, title, icon: Icon, note }) => {
      const field = fields.find(item => item.id === id);
      if (!field) return null;
      return <article className="osk-settings-card" key={id}><header><Icon size={19} /><h2>{title}</h2></header><p>{note}</p><DashboardFieldControl field={field} value={draft[id]} guildOptions={guildOptions} onChange={onChange} /></article>;
    })}</div>
  </div>;
}
