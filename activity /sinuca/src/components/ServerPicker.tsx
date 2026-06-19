import { ArrowRight, Plus, RefreshCw, ServerCrash } from "lucide-react";
import type { DashboardServerCard, DashboardUserPayload } from "../types/dashboard";
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
}

export function ServerPicker({
  user,
  manageable,
  needsInvite,
  loading,
  onSelect,
  onInvite,
  onRefresh,
  onLogout,
}: ServerPickerProps) {
  const name = user?.global_name || user?.username || "sua conta";

  return (
    <div className="osk-browser">
      <nav className="osk-browser-nav">
        <a className="osk-browser-brand" href="#">
          <span className="osk-browser-brand-mark">OK</span>
          <span className="osk-browser-brand-text">osaka.dashboard</span>
        </a>
        <div className="osk-browser-nav-actions">
          <button className="osk-btn osk-btn--sm" onClick={onRefresh} disabled={loading}>
            <RefreshCw size={14} className={loading ? "osk-spin" : undefined} />
            Atualizar
          </button>
          <button className="osk-btn osk-btn--sm osk-btn--ghost" onClick={onLogout}>
            Sair
          </button>
        </div>
      </nav>

      <section className="osk-picker-head">
        <div>
          <span className="osk-hero-eyebrow">Servidores</span>
          <h1>Escolha onde configurar.</h1>
          <p>
            {name} pode configurar os servidores ativos abaixo. Servidores sem o bot aparecem
            desativados para convite rápido.
          </p>
        </div>
      </section>

      <section className="osk-server-group">
        <div className="osk-server-group-head">
          <h2>Com o bot instalado</h2>
          <span className="osk-badge" data-state="ready">
            {manageable.length}
          </span>
        </div>
        <div className="osk-server-grid">
          {manageable.map((s) => (
            <button key={s.id} className="osk-server-card" onClick={() => onSelect(s)}>
              <SmartAvatar className="osk-server-avatar" src={s.icon} name={s.name} type="server" alt={s.name} />
              <span>
                <strong>{s.name}</strong>
                <small>{s.owner ? "Você é dono" : "Staff autorizado"}</small>
              </span>
              <span className="osk-server-cta">
                <span className="osk-server-cta-label">Configurar</span> <ArrowRight size={13} />
              </span>
            </button>
          ))}
          {!loading && manageable.length === 0 && (
            <div className="osk-empty">
              <ServerCrash size={18} />
              <span>Nenhum servidor configurável encontrado.</span>
            </div>
          )}
        </div>
      </section>

      <section className="osk-server-group">
        <div className="osk-server-group-head">
          <h2>Sem o bot ainda</h2>
          <span className="osk-badge">{needsInvite.length}</span>
        </div>
        <div className="osk-server-grid">
          {needsInvite.map((s) => (
            <button
              key={s.id}
              className="osk-server-card"
              data-disabled="true"
              onClick={() => onInvite(s)}
            >
              <SmartAvatar className="osk-server-avatar" src={s.icon} name={s.name} type="server" alt={s.name} />
              <span>
                <strong>{s.name}</strong>
                <small>O bot ainda não está neste servidor</small>
              </span>
              <span className="osk-server-cta">
                <span className="osk-server-cta-label">Convidar</span> <Plus size={13} />
              </span>
            </button>
          ))}
          {!loading && needsInvite.length === 0 && (
            <div className="osk-empty">
              <ServerCrash size={18} />
              <span>Nenhum servidor pendente de convite.</span>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
