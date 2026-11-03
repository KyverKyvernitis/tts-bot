import { ArrowRight, ChevronDown, Plus, RefreshCw, Search, Server, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import type {
  DashboardServerCard,
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { Brand } from "./BrowserLanding";
import { SmartAvatar } from "./SmartAvatar";

interface ServerPickerProps {
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  loading: boolean;
  loaded?: boolean;
  error?: string | null;
  user: DashboardUserPayload;
  bot: DashboardUserPayload | null;
  supportServer: DashboardSupportServerPayload | null;
  onSelect(server: DashboardServerCard): void;
  onInvite(server: DashboardServerCard): void;
  onRefresh(): void;
  onLogout(): void;
  onHome(): void;
}

export function ServerPicker({
  manageable,
  needsInvite,
  loading,
  loaded,
  error,
  user,
  bot,
  supportServer,
  onSelect,
  onInvite,
  onRefresh,
  onLogout,
  onHome,
}: ServerPickerProps) {
  const [query, setQuery] = useState("");
  const [showInvites, setShowInvites] = useState(false);
  const [collapsedQuery, setCollapsedQuery] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const seenLoading = useRef(loading);
  const [loadedOnce, setLoadedOnce] = useState(manageable.length + needsInvite.length > 0);
  const normalizedQuery = normalizeServerSearch(query.trim());
  const searching = normalizedQuery.length > 0;
  const filteredManageable = useMemo(() => filterServers(manageable, normalizedQuery), [manageable, normalizedQuery]);
  const filteredInvites = useMemo(() => filterServers(needsInvite, normalizedQuery), [needsInvite, normalizedQuery]);
  const hasServers = manageable.length + needsInvite.length > 0;
  const initialLoading = !error && !hasServers && !(loaded ?? loadedOnce) && (loading || loaded === false);
  const invitesExpanded = searching ? collapsedQuery !== normalizedQuery : showInvites;
  const resultCount = filteredManageable.length + filteredInvites.length;

  useEffect(() => {
    if (loading) seenLoading.current = true;
    else if (seenLoading.current || hasServers) setLoadedOnce(true);
  }, [loading, hasServers]);

  function clearSearch() {
    setQuery("");
    setCollapsedQuery(null);
    searchRef.current?.focus({ preventScroll: true });
  }

  function toggleInvites() {
    if (searching) setCollapsedQuery(invitesExpanded ? normalizedQuery : null);
    else setShowInvites((value) => !value);
  }

  return (
    <div className="osk-picker-shell" data-workspace="true">
      <header className="osk-picker-nav">
        <button type="button" className="osk-brand-button" onClick={onHome} aria-label="Página inicial"><Brand bot={bot} /></button>
        <AccountMenu
          user={user}
          busy={loading}
          supportInviteUrl={supportServer?.inviteUrl}
          showServersAction={false}
          onServers={() => undefined}
          onRefresh={onRefresh}
          onLogout={onLogout}
        />
      </header>

      <main className="osk-picker-main" aria-label="Selecionar servidor">
        <header className="osk-picker-heading osk-picker-heading--simple">
          <div><h1>Seus servidores</h1><p>Escolha onde configurar o bot.</p></div>
          <button type="button" className="osk-picker-refresh" onClick={onRefresh} disabled={loading} aria-label="Atualizar servidores" title="Atualizar servidores"><RefreshCw size={16} aria-hidden="true" /><span>Atualizar</span></button>
        </header>

        <div className="osk-server-search" role="search">
          <Search size={19} aria-hidden="true" />
          <input ref={searchRef} value={query} onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)} placeholder="Buscar servidor" aria-label="Buscar servidor" autoComplete="off" spellCheck={false} />
          {query && <button type="button" onClick={clearSearch} aria-label="Limpar busca"><X size={17} aria-hidden="true" /></button>}
        </div>
        <div className="osk-picker-list-status" role="status" aria-live="polite">
          {loading ? <><RefreshCw size={13} aria-hidden="true" />{initialLoading ? "Carregando servidores…" : "Atualizando servidores…"}</> : searching ? `${resultCount} ${resultCount === 1 ? "servidor encontrado" : "servidores encontrados"}` : "Sua conta precisa ter permissão para administrar o servidor."}
        </div>

        {error && !loading && <div className="osk-picker-load-error" role="alert"><span><strong>Não foi possível atualizar os servidores.</strong><small>{error}</small></span><button type="button" onClick={onRefresh}>Tentar novamente</button></div>}

        <ServerGroup title="Com o bot" count={filteredManageable.length} loading={initialLoading}>
          {filteredManageable.map((server) => <ServerCard key={server.id} server={server} action="Configurar" onClick={() => onSelect(server)} />)}
          {!initialLoading && !error && filteredManageable.length === 0 && <EmptyState text={searching ? "Nenhum servidor com o bot corresponde à busca." : "O bot ainda não está em nenhum dos seus servidores."} detail={searching ? undefined : needsInvite.length ? "Você pode adicioná-lo a um servidor abaixo." : "Confira se sua conta tem permissão para administrar um servidor."} />}
          {initialLoading && <ServerSkeletons />}
        </ServerGroup>

        {needsInvite.length > 0 && <section className="osk-picker-invite-section">
          <button type="button" className="osk-picker-invite-toggle" data-open={invitesExpanded || undefined} aria-expanded={invitesExpanded} aria-controls="osk-picker-invite-list" onClick={toggleInvites}>
            <span><strong>Adicionar a outro servidor</strong><small>Servidores que você pode administrar</small></span>
            <em>{filteredInvites.length}</em>
            <ChevronDown size={18} aria-hidden="true" />
          </button>
          <div className="osk-picker-grid osk-picker-invite-grid" id="osk-picker-invite-list" hidden={!invitesExpanded}>
            {filteredInvites.map((server) => <ServerCard key={server.id} server={server} action="Adicionar" invite onClick={() => onInvite(server)} />)}
            {filteredInvites.length === 0 && <EmptyState text="Nenhum outro servidor corresponde à busca." />}
          </div>
        </section>}
      </main>
    </div>
  );
}

export function normalizeServerSearch(value: string) {
  return value.normalize("NFKD").replace(/\p{M}/gu, "").toLocaleLowerCase("pt-BR");
}

function filterServers(servers: DashboardServerCard[], query: string) {
  if (!query) return servers;
  return servers.filter((server) => normalizeServerSearch(server.name).includes(query));
}

function ServerGroup({ title, count, loading, children }: { title: string; count: number; loading: boolean; children: ReactNode }) {
  return <section className="osk-picker-group" aria-label={title} aria-busy={loading}><header><h2>{title}</h2>{!loading && <span>{count}</span>}</header><div className="osk-picker-grid">{children}</div></section>;
}

function ServerCard({ server, action, invite = false, onClick }: { server: DashboardServerCard; action: string; invite?: boolean; onClick(): void }) {
  return <button type="button" className="osk-picker-card" data-invite={invite || undefined} onClick={onClick} aria-label={`${action} ${server.name}`}>
    <SmartAvatar className="osk-picker-server-avatar" src={server.icon} name={server.name} type="server" alt="" size={48} />
    <span className="osk-picker-server-copy"><strong title={server.name}>{server.name}</strong><small>{invite ? "Adicionar o bot" : "Abrir configurações"}</small></span>
    <span className="osk-picker-card-action" aria-hidden="true">{invite ? <Plus size={16} /> : <ArrowRight size={16} />}<span>{action}</span></span>
  </button>;
}

function EmptyState({ text, detail }: { text: string; detail?: string }) {
  return <div className="osk-picker-empty"><Server size={21} aria-hidden="true" /><span><strong>{text}</strong>{detail && <small>{detail}</small>}</span></div>;
}

function ServerSkeletons() {
  return <>{[0, 1, 2].map((item) => <div className="osk-picker-card osk-skeleton-card" aria-hidden="true" key={item}><i /><span><i /><i /></span></div>)}</>;
}
