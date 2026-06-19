import { ArrowRight, Bot, ClipboardList, DoorOpen, Mic, ScrollText, Ticket } from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";
import { SmartAvatar } from "./SmartAvatar";

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
      <BrowserNav loggedIn={loggedIn} userName={name} userAvatar={user?.avatarUrl} onLogin={onLogin} onDashboard={onDashboard} />

      <section className="osk-browser-hero">
        <div>
          <h1>
            Painel <span>de controle</span>
          </h1>
          <p>
            Configure tickets, boas-vindas, aniversários, TTS, música, logs e jogos pelo painel.
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

        <div className="osk-preview" aria-hidden="true">
          <div className="osk-preview-bar">
            <i /><i /><i />
            <span className="osk-preview-url">osaka.dashboard/painel</span>
          </div>
          <div className="osk-preview-body">
            <div className="osk-preview-nav">
              <span className="osk-preview-nav-brand">
                <span />
                osaka
              </span>
              <span className="osk-preview-nav-item" data-active="true">
                <DoorOpen size={12} /> Boas-vindas
              </span>
              <span className="osk-preview-nav-item">
                <Ticket size={12} /> Tickets
              </span>
              <span className="osk-preview-nav-item">
                <Mic size={12} /> TTS
              </span>
              <span className="osk-preview-nav-item">
                <Bot size={12} /> Chatbot IA
              </span>
            </div>
            <div className="osk-preview-content">
              <span className="osk-preview-pill" />
              <div className="osk-preview-card">
                <span className="osk-preview-card-icon" data-state="ready" />
                <span className="osk-preview-card-lines">
                  <span /><span />
                </span>
                <span className="osk-preview-card-badge" data-state="ready">Pronto</span>
              </div>
              <div className="osk-preview-card">
                <span className="osk-preview-card-icon" data-state="partial" />
                <span className="osk-preview-card-lines">
                  <span /><span />
                </span>
                <span className="osk-preview-card-badge" data-state="partial">3/5</span>
              </div>
              <div className="osk-preview-card">
                <span className="osk-preview-card-icon" data-state="pending" />
                <span className="osk-preview-card-lines">
                  <span /><span />
                </span>
                <span className="osk-preview-card-badge" data-state="pending">Configurar</span>
              </div>
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

    </div>
  );
}

function BrowserNav({
  loggedIn,
  userName,
  userAvatar,
  onLogin,
  onDashboard,
}: {
  loggedIn: boolean;
  userName: string;
  userAvatar?: string | null;
  onLogin(): void;
  onDashboard(): void;
}) {
  return (
    <nav className="osk-browser-nav">
      <a className="osk-browser-brand" href="#">
        <span className="osk-browser-brand-mark">OK</span>
        <span className="osk-browser-brand-text">osaka.dashboard</span>
      </a>
      <div className="osk-browser-nav-actions">
        {loggedIn ? (
          <button className="osk-btn osk-btn--sm" onClick={onDashboard}>
            <SmartAvatar
              className="osk-user-chip-avatar"
              src={userAvatar}
              name={userName}
              type="user"
              alt={userName}
              size={22}
            />
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
