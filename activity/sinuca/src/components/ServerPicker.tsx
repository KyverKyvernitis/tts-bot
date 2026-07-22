import { ArrowRight, LogOut, Plus, RefreshCw, Server, ServerCrash } from "lucide-react";
import type { DashboardServerCard, DashboardUserPayload } from "../types/dashboard";
import { Brand } from "./BrowserLanding";
import { SmartAvatar } from "./SmartAvatar";

interface ServerPickerProps {
  user: DashboardUserPayload | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  loading: boolean;
  onSelect(server: DashboardServerCard): void;
  onInvite(server: DashboardServerCard): void;
  onRefresh(): void;
  onLogout(): void;
  onHome(): void;
}

export function ServerPicker({ user, manageable, needsInvite, loading, onSelect, onInvite, onRefresh, onLogout, onHome }: ServerPickerProps) {
  const name = user?.global_name || user?.username || "sua conta";
  return (
    <div className="osk-picker-shell">
      <header className="osk-picker-nav">
        <button className="osk-brand-button" onClick={onHome}><Brand /></button>
        <div>
          <button className="osk-icon-text-button" onClick={onRefresh} disabled={loading}><RefreshCw size={15} className={loading ? "osk-spin" : undefined} />Atualizar</button>
          <button className="osk-icon-text-button" onClick={onLogout}><LogOut size={15} />Sair</button>
        </div>
      </header>

      <main className="osk-picker-main">
        <header className="osk-picker-heading">
          <div><span className="osk-kicker"><Server size={14} /> Servidores autorizados</span><h1>Onde você quer configurar a Osaka?</h1><p>Olá, {name}. Mostramos apenas servidores em que sua conta possui permissão adequada.</p></div>
          {user && <SmartAvatar className="osk-picker-user" src={user.avatarUrl} name={name} type="user" alt={name} size={58} />}
        </header>

        <ServerGroup title="Prontos para configurar" count={manageable.length} tone="ready">
          {manageable.map((server) => <ServerCard key={server.id} server={server} action="Configurar" onClick={() => onSelect(server)} />)}
          {!loading && manageable.length === 0 && <EmptyState text="Nenhum servidor com a Osaka instalado foi encontrado." />}
          {loading && <ServerSkeletons />}
        </ServerGroup>

        {needsInvite.length > 0 && <ServerGroup title="Instale a Osaka primeiro" count={needsInvite.length} tone="neutral">
          {needsInvite.map((server) => <ServerCard key={server.id} server={server} action="Convidar" invite onClick={() => onInvite(server)} />)}
        </ServerGroup>}
      </main>
    </div>
  );
}

function ServerGroup({ title, count, tone, children }: { title: string; count: number; tone: "ready" | "neutral"; children: React.ReactNode }) {
  return <section className="osk-picker-group"><header><h2>{title}</h2><span data-tone={tone}>{count}</span></header><div className="osk-picker-grid">{children}</div></section>;
}

function ServerCard({ server, action, invite = false, onClick }: { server: DashboardServerCard; action: string; invite?: boolean; onClick(): void }) {
  return <button className="osk-picker-card" data-invite={invite || undefined} onClick={onClick}>
    <SmartAvatar className="osk-picker-server-avatar" src={server.icon} name={server.name} type="server" alt={server.name} size={54} />
    <span className="osk-picker-server-copy"><strong>{server.name}</strong><small>{server.owner ? "Você é o proprietário" : invite ? "O bot ainda não está instalado" : "Você pode gerenciar este servidor"}</small></span>
    <span className="osk-picker-card-action">{invite ? <Plus size={15} /> : null}{action}{!invite ? <ArrowRight size={15} /> : null}</span>
  </button>;
}

function EmptyState({ text }: { text: string }) {
  return <div className="osk-picker-empty"><ServerCrash size={22} /><span>{text}</span></div>;
}

function ServerSkeletons() {
  return <>{[0, 1, 2].map((item) => <div className="osk-picker-card osk-skeleton-card" key={item}><i /><span><i /><i /></span></div>)}</>;
}
