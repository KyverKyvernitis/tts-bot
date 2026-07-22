import { ArrowRight, ChevronDown, LogOut, Plus, RefreshCw, Search, ServerCrash, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { ChangeEvent } from "react";
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
  const [query, setQuery] = useState("");
  const [showInvites, setShowInvites] = useState(false);
  const normalizedQuery = query.trim().toLocaleLowerCase("pt-BR");
  const filteredManageable = useMemo(() => filterServers(manageable, normalizedQuery), [manageable, normalizedQuery]);
  const filteredInvites = useMemo(() => filterServers(needsInvite, normalizedQuery), [needsInvite, normalizedQuery]);

  return (
    <div className="osk-picker-shell">
      <header className="osk-picker-nav">
        <button className="osk-brand-button" onClick={onHome}><Brand /></button>
        <div>
          <button className="osk-icon-text-button" onClick={onRefresh} disabled={loading} aria-label="Atualizar servidores"><RefreshCw size={16} className={loading ? "osk-spin" : undefined} /><span>Atualizar</span></button>
          <button className="osk-icon-text-button" onClick={onLogout} aria-label="Sair"><LogOut size={16} /><span>Sair</span></button>
        </div>
      </header>

      <main className="osk-picker-main">
        <header className="osk-picker-heading osk-picker-heading--simple">
          <div><span className="osk-kicker">Olá, {name}</span><h1>Escolha um servidor.</h1><p>Você verá apenas servidores que pode administrar.</p></div>
          {user && <SmartAvatar className="osk-picker-user" src={user.avatarUrl} name={name} type="user" alt={name} size={52} />}
        </header>

        <label className="osk-server-search">
          <Search size={18} />
          <input value={query} onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)} placeholder="Buscar servidor" aria-label="Buscar servidor" />
          {query && <button type="button" onClick={() => setQuery("")} aria-label="Limpar busca"><X size={16} /></button>}
        </label>

        <ServerGroup title="Com a Osaka" count={filteredManageable.length}>
          {filteredManageable.map((server) => <ServerCard key={server.id} server={server} action="Configurar" onClick={() => onSelect(server)} />)}
          {!loading && filteredManageable.length === 0 && <EmptyState text={query ? "Nenhum servidor encontrado." : "Nenhum servidor com a Osaka foi encontrado."} />}
          {loading && <ServerSkeletons />}
        </ServerGroup>

        {needsInvite.length > 0 && <section className="osk-picker-invite-section">
          <button className="osk-picker-invite-toggle" data-open={showInvites || undefined} onClick={() => setShowInvites((value) => !value)}>
            <span><strong>Outros servidores</strong><small>Instale a Osaka para começar</small></span>
            <em>{filteredInvites.length}</em>
            <ChevronDown size={18} />
          </button>
          {showInvites && <div className="osk-picker-grid osk-picker-invite-grid">
            {filteredInvites.map((server) => <ServerCard key={server.id} server={server} action="Instalar" invite onClick={() => onInvite(server)} />)}
            {filteredInvites.length === 0 && <EmptyState text="Nenhum outro servidor encontrado." />}
          </div>}
        </section>}
      </main>
    </div>
  );
}

function filterServers(servers: DashboardServerCard[], query: string) {
  if (!query) return servers;
  return servers.filter((server) => server.name.toLocaleLowerCase("pt-BR").includes(query));
}

function ServerGroup({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return <section className="osk-picker-group"><header><h2>{title}</h2><span>{count}</span></header><div className="osk-picker-grid">{children}</div></section>;
}

function ServerCard({ server, action, invite = false, onClick }: { server: DashboardServerCard; action: string; invite?: boolean; onClick(): void }) {
  return <button className="osk-picker-card" data-invite={invite || undefined} onClick={onClick}>
    <SmartAvatar className="osk-picker-server-avatar" src={server.icon} name={server.name} type="server" alt={server.name} size={50} />
    <span className="osk-picker-server-copy"><strong>{server.name}</strong><small>{invite ? "A Osaka ainda não está neste servidor" : "Pronto para configurar"}</small></span>
    <span className="osk-picker-card-action">{invite ? <Plus size={15} /> : null}<span>{action}</span>{!invite ? <ArrowRight size={15} /> : null}</span>
  </button>;
}

function EmptyState({ text }: { text: string }) {
  return <div className="osk-picker-empty"><ServerCrash size={22} /><span>{text}</span></div>;
}

function ServerSkeletons() {
  return <>{[0, 1, 2].map((item) => <div className="osk-picker-card osk-skeleton-card" key={item}><i /><span><i /><i /></span></div>)}</>;
}
