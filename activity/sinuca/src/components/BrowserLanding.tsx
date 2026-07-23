import {
  ArrowRight,
  Cake,
  ClipboardList,
  DoorOpen,
  Palette,
  Sparkles,
  Ticket,
  Volume2,
} from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
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

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="osk-brand-lockup" data-compact={compact || undefined}>
      <span className="osk-brand-symbol" aria-hidden="true"><Sparkles size={18} /></span>
      <span className="osk-brand-copy"><strong>Osaka</strong><small>Painel</small></span>
    </span>
  );
}

export function BrowserLanding({ loggedIn, onLogin, onDashboard, onNavigate }: BrowserLandingProps) {
  const primaryAction = loggedIn ? onDashboard : onLogin;

  return (
    <div className="osk-minimal-landing">
      <header className="osk-minimal-nav">
        <button className="osk-brand-button" onClick={() => onNavigate("/")} aria-label="Página inicial">
          <Brand />
        </button>
        <button className="osk-minimal-nav-action" onClick={primaryAction}>
          <span>{loggedIn ? "Painel" : "Entrar"}</span>
          <ArrowRight size={15} />
        </button>
      </header>

      <main className="osk-minimal-main">
        <section className="osk-minimal-hero">
          <h1>
            <span>Configure a Osaka.</span>
            <strong>Sem comandos.</strong>
          </h1>
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
      </main>

      <footer className="osk-minimal-footer">
        <Brand compact />
        <nav aria-label="Links legais">
          <button onClick={() => onNavigate("/privacy")}>Privacidade</button>
          <button onClick={() => onNavigate("/terms")}>Termos</button>
        </nav>
      </footer>
    </div>
  );
}
