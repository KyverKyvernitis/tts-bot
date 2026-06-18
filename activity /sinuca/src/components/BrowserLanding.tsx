import { ArrowRight, Cake, Cpu, DoorOpen, HardDrive, Mic, Music, Sparkles, Ticket, UploadCloud } from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";
import { guildInitials } from "../moduleCatalog";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
  message?: string;
  busy?: boolean;
  onLogin(): void;
  onDashboard(): void;
}

export function BrowserLanding({ loggedIn, user, message, busy, onLogin, onDashboard }: BrowserLandingProps) {
  const name = user?.global_name || user?.username || "Conta Discord";
  return (
    <div className="osk-browser">
      <BrowserNav loggedIn={loggedIn} userName={name} busy={busy} onLogin={onLogin} onDashboard={onDashboard} />

      <section className="osk-browser-hero">
        <div>
          <span className="osk-hero-eyebrow-lg">Dashboard do bot</span>
          <h1>
            Seu servidor, do seu jeito — <span>sem comandos soltos.</span>
          </h1>
          <p>
            Configure tickets, boas-vindas, TTS, música, workers, updates e VPS em um painel visual,
            direto no Discord ou pelo navegador.
          </p>
          <div className="osk-hero-actions">
            <button className="osk-btn osk-btn--primary osk-btn--lg" onClick={loggedIn ? onDashboard : onLogin} disabled={busy}>
              {loggedIn ? "Abrir Dashboard" : "Entrar com Discord"}
              <ArrowRight size={16} />
            </button>
            <a className="osk-btn osk-btn--lg" href="#features">
              Ver módulos
            </a>
          </div>
          {message && <div className="osk-status">{message}</div>}
        </div>

        <div className="osk-preview" aria-hidden>
          <div className="osk-preview-bar">
            <i /><i /><i />
          </div>
          <div className="osk-preview-body">
            <div className="osk-preview-side">
              <span /><span /><span /><span /><span />
            </div>
            <div className="osk-preview-main">
              <span className="osk-preview-pill" />
              <div className="osk-preview-row" />
              <div className="osk-preview-row" />
              <div className="osk-preview-row" />
            </div>
          </div>
        </div>
      </section>

      <section id="features" className="osk-features">
        {[
          { icon: Ticket, title: "Tickets", text: "Painéis, opções, cargos staff e permissões de atendimento." },
          { icon: DoorOpen, title: "Boas-vindas", text: "Mensagem, embed, imagem, webhook e cargo automático." },
          { icon: Cake, title: "Aniversários", text: "Calendário público, cargo do dia e mensagens automáticas." },
          { icon: Music, title: "Música", text: "Fila, player, permissões de DJ e preferências de áudio." },
          { icon: Mic, title: "TTS", text: "Vozes, canais, limites, velocidade e comportamento de leitura." },
          { icon: Cpu, title: "Workers", text: "Estado dos workers, filas, capacidade e tarefas em segundo plano." },
          { icon: UploadCloud, title: "Updates", text: "Canal de update, estado do auto update e avisos do bot." },
          { icon: HardDrive, title: "VPS", text: "Saúde do servidor, recursos e monitoramento da máquina." },
        ].map((f) => (
          <article className="osk-feature" key={f.title}>
            <div className="osk-feature-icon">
              <f.icon size={20} />
            </div>
            <h3>{f.title}</h3>
            <p>{f.text}</p>
          </article>
        ))}
      </section>

      <section className="osk-flow">
        <span className="osk-hero-eyebrow">
          <Sparkles size={12} /> Como funciona
        </span>
        <h2>Mesmo painel, aberto do jeito certo.</h2>
        <div className="osk-flow-grid">
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">1</span>
            <strong>Dentro do Discord</strong>
            <p>Abre direto no servidor atual, sem exigir escolha inicial.</p>
          </div>
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">2</span>
            <strong>No navegador</strong>
            <p>Faz login, escolhe o servidor e entra no mesmo painel.</p>
          </div>
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">3</span>
            <strong>Bot ausente</strong>
            <p>Servidores sem o bot aparecem apagados e levam para o convite.</p>
          </div>
        </div>
      </section>
    </div>
  );
}

function BrowserNav({
  loggedIn,
  userName,
  busy,
  onLogin,
  onDashboard,
}: {
  loggedIn: boolean;
  userName: string;
  busy?: boolean;
  onLogin(): void;
  onDashboard(): void;
}) {
  return (
    <nav className="osk-browser-nav">
      <a className="osk-browser-brand" href="#">
        <span className="osk-browser-brand-mark">OK</span>
        osaka.dashboard
      </a>
      <div className="osk-browser-nav-actions">
        {loggedIn ? (
          <button className="osk-btn osk-btn--sm" onClick={onDashboard} disabled={busy}>
            <span className="osk-user-chip-avatar" style={{ width: 22, height: 22, borderRadius: "50%" }}>
              {guildInitials(userName)}
            </span>
            Dashboard
          </button>
        ) : (
          <button className="osk-btn osk-btn--primary osk-btn--sm" onClick={onLogin} disabled={busy}>
            Entrar com Discord
          </button>
        )}
      </div>
    </nav>
  );
}
