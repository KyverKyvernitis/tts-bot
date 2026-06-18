import { ArrowRight, Bot, ClipboardList, DoorOpen, Mic, ScrollText, Sparkles, Ticket } from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";
import { guildInitials } from "../moduleCatalog";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
  onLogin(): void;
  onDashboard(): void;
}

export function BrowserLanding({ loggedIn, user, onLogin, onDashboard }: BrowserLandingProps) {
  const name = user?.global_name || user?.username || "Conta Discord";
  return (
    <div className="osk-browser">
      <BrowserNav loggedIn={loggedIn} userName={name} onLogin={onLogin} onDashboard={onDashboard} />

      <section className="osk-browser-hero">
        <div>
          <span className="osk-hero-eyebrow-lg">Activity + Web</span>
          <h1>
            Seu servidor, do seu jeito — <span>sem abrir documentação.</span>
          </h1>
          <p>
            Tickets, boas-vindas, TTS, música, IA, logs, jogos e mais. Clica, ajusta e salva —
            direto dentro do Discord ou aqui no navegador.
          </p>
          <div className="osk-hero-actions">
            <button className="osk-btn osk-btn--primary osk-btn--lg" onClick={loggedIn ? onDashboard : onLogin}>
              {loggedIn ? "Abrir Dashboard" : "Entrar com Discord"}
              <ArrowRight size={16} />
            </button>
            <a className="osk-btn osk-btn--lg" href="#features">
              Ver tudo que dá pra mexer
            </a>
          </div>
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
          { icon: Ticket, title: "Tickets", text: "Monta o painel, define a staff e guarda o histórico de cada atendimento." },
          { icon: DoorOpen, title: "Boas-vindas", text: "Mensagem, embed e cargo automático pra quem chega no servidor." },
          { icon: Mic, title: "TTS", text: "Voz nos canais, com limites, idioma e prefixos do seu jeito." },
          { icon: Bot, title: "Chatbot IA", text: "Vários modelos, personalidades diferentes e canais dedicados pra papear." },
          { icon: ClipboardList, title: "Formulários", text: "Pergunta o que precisar e recebe a resposta organizada num embed." },
          { icon: ScrollText, title: "Logs", text: "Tudo que acontece no servidor num canal só, sem se perder." },
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
        <h2>Cada lugar tem um jeito diferente de abrir.</h2>
        <div className="osk-flow-grid">
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">1</span>
            <strong>Dentro do Discord</strong>
            <p>Abre na hora no servidor em que você ativou a Activity.</p>
          </div>
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">2</span>
            <strong>No navegador</strong>
            <p>Faz login, escolhe o servidor e cai direto no painel.</p>
          </div>
          <div className="osk-flow-item">
            <span className="osk-flow-item-step">3</span>
            <strong>Sem o bot ainda?</strong>
            <p>O servidor aparece apagado e te leva pra tela de convite num clique.</p>
          </div>
        </div>
      </section>
    </div>
  );
}

function BrowserNav({
  loggedIn,
  userName,
  onLogin,
  onDashboard,
}: {
  loggedIn: boolean;
  userName: string;
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
          <button className="osk-btn osk-btn--sm" onClick={onDashboard}>
            <span className="osk-user-chip-avatar" style={{ width: 22, height: 22, borderRadius: "50%" }}>
              {guildInitials(userName)}
            </span>
            Dashboard
          </button>
        ) : (
          <button className="osk-btn osk-btn--primary osk-btn--sm" onClick={onLogin}>
            Entrar com Discord
          </button>
        )}
      </div>
    </nav>
  );
}
