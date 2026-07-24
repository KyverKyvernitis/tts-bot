import {
  ArrowRight,
  Cake,
  ChevronRight,
  ClipboardList,
  DoorOpen,
  LogIn,
  MessagesSquare,
  Palette,
  Sparkles,
  Ticket,
  Volume2,
} from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";
import { SmartAvatar } from "./SmartAvatar";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
  bot: DashboardUserPayload | null;
  onLogin(): void;
  onDashboard(): void;
  onNavigate(path: string): void;
}

const features = [
  { icon: DoorOpen, title: "Boas-vindas" },
  { icon: Ticket, title: "Tickets" },
  { icon: ClipboardList, title: "Formulários" },
  { icon: Palette, title: "Cargos de cor" },
  { icon: Cake, title: "Aniversários" },
  { icon: Volume2, title: "TTS" },
];

function displayName(identity: DashboardUserPayload | null, fallback: string) {
  return identity?.global_name?.trim() || identity?.username?.trim() || fallback;
}

export function Brand({
  compact = false,
  bot = null,
}: {
  compact?: boolean;
  bot?: DashboardUserPayload | null;
}) {
  const botName = displayName(bot, "Osaka");
  return (
    <span className="osk-brand-lockup" data-compact={compact || undefined}>
      {bot?.avatarUrl ? (
        <SmartAvatar
          className="osk-brand-symbol osk-brand-bot-avatar"
          src={bot.avatarUrl}
          name={botName}
          type="user"
          alt={`Avatar da ${botName}`}
          size={compact ? 34 : 40}
        />
      ) : <span className="osk-brand-symbol" aria-hidden="true"><Sparkles size={18} /></span>}
      <span className="osk-brand-copy"><strong>Osaka</strong><small>Painel</small></span>
    </span>
  );
}

export function BrowserLanding({ loggedIn, user, bot, onLogin, onDashboard, onNavigate }: BrowserLandingProps) {
  const primaryAction = loggedIn ? onDashboard : onLogin;
  const userName = displayName(user, "Conta");

  return (
    <div className="osk-minimal-landing">
      <header className="osk-minimal-nav">
        <button className="osk-brand-button" onClick={() => onNavigate("/")} aria-label="Página inicial">
          <Brand bot={bot} />
        </button>
        <button
          className="osk-minimal-account"
          data-logged-in={loggedIn || undefined}
          onClick={primaryAction}
          aria-label={loggedIn ? `Abrir painel como ${userName}` : "Entrar com Discord"}
        >
          {loggedIn && user ? (
            <SmartAvatar
              className="osk-minimal-account-avatar"
              src={user.avatarUrl}
              name={userName}
              type="user"
              alt={`Avatar de ${userName}`}
              size={32}
            />
          ) : <span className="osk-minimal-account-login" aria-hidden="true"><LogIn size={16} /></span>}
          <span className="osk-minimal-account-copy">
            <small>{loggedIn ? "Conectado" : "Discord"}</small>
            <strong>{loggedIn ? userName : "Entrar"}</strong>
          </span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
      </header>

      <main className="osk-minimal-main">
        <section className="osk-minimal-hero">
          <h1><strong>Dashboard</strong></h1>
          <p>Escolha um servidor, ajuste as funções e salve.</p>
          <button className="osk-primary-button osk-minimal-primary-action" onClick={primaryAction}>
            {loggedIn ? "Abrir painel" : "Entrar com Discord"}
            <ArrowRight size={17} />
          </button>
        </section>

        <section className="osk-minimal-features" aria-labelledby="landing-features-title">
          <h2 id="landing-features-title">Funções</h2>
          <div className="osk-minimal-feature-grid">
            {features.map((feature) => (
              <article key={feature.title}>
                <span aria-hidden="true"><feature.icon size={20} /></span>
                <h3>{feature.title}</h3>
              </article>
            ))}
          </div>
        </section>

        <a
          className="osk-support-card"
          href="https://discord.gg/RckuzJbvVk"
          target="_blank"
          rel="noreferrer noopener"
        >
          <span className="osk-support-card-icon" aria-hidden="true"><MessagesSquare size={20} /></span>
          <span className="osk-support-card-copy">
            <strong>Servidor de suporte</strong>
            <small>Dúvidas, problemas e novidades da Osaka.</small>
          </span>
          <span className="osk-support-card-action">Entrar <ArrowRight size={15} /></span>
        </a>
      </main>
    </div>
  );
}
