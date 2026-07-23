import {
  ArrowRight,
  Cake,
  Check,
  ClipboardList,
  DoorOpen,
  Palette,
  ShieldCheck,
  Sparkles,
  Ticket,
  Volume2,
} from "lucide-react";
import type { DashboardUserPayload } from "../types/dashboard";
import { SmartAvatar } from "./SmartAvatar";

interface BrowserLandingProps {
  loggedIn: boolean;
  user: DashboardUserPayload | null;
  onLogin(): void;
  onDashboard(): void;
  onNavigate(path: string): void;
}

const features = [
  { icon: DoorOpen, title: "Boas-vindas", text: "Mensagens, cargos e aparência." },
  { icon: Ticket, title: "Tickets", text: "Atendimento, equipe e permissões." },
  { icon: ClipboardList, title: "Formulários", text: "Perguntas, respostas e aprovação." },
  { icon: Palette, title: "Cargos de cor", text: "Painéis e cargos personalizados." },
  { icon: Cake, title: "Aniversários", text: "Cadastro, calendário e avisos." },
  { icon: Volume2, title: "TTS", text: "Voz, canais e regras de leitura." },
];

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="osk-brand-lockup" data-compact={compact || undefined}>
      <span className="osk-brand-symbol" aria-hidden="true"><Sparkles size={18} /></span>
      <span className="osk-brand-copy"><strong>Osaka</strong><small>Painel</small></span>
    </span>
  );
}

export function BrowserLanding({ loggedIn, user, onLogin, onDashboard, onNavigate }: BrowserLandingProps) {
  const name = user?.global_name || user?.username || "Conta Discord";
  const primaryAction = loggedIn ? onDashboard : onLogin;

  return (
    <div className="osk-public-shell">
      <header className="osk-public-nav">
        <button className="osk-brand-button" onClick={() => onNavigate("/")} aria-label="Página inicial"><Brand /></button>
        <nav aria-label="Navegação principal">
          <a href="#funcoes">Funções</a>
          <button onClick={() => onNavigate("/privacy")}>Privacidade</button>
          <button onClick={() => onNavigate("/terms")}>Termos</button>
        </nav>
        {loggedIn ? (
          <button className="osk-account-button" onClick={onDashboard}>
            <SmartAvatar className="osk-account-avatar" src={user?.avatarUrl} name={name} type="user" alt={name} size={28} />
            <span>Abrir painel</span>
            <ArrowRight size={15} />
          </button>
        ) : (
          <button className="osk-primary-button osk-primary-button--small" onClick={onLogin}>Entrar</button>
        )}
      </header>

      <main>
        <section className="osk-public-hero osk-public-hero--simple">
          <div className="osk-public-hero-copy">
            <span className="osk-kicker"><ShieldCheck size={14} /> Painel oficial da Osaka</span>
            <h1>Configure a Osaka <span>sem comandos.</span></h1>
            <p>Escolha um servidor, ajuste somente o que precisa e salve. O painel mantém tudo organizado em um só lugar.</p>
            <div className="osk-public-actions">
              <button className="osk-primary-button" onClick={primaryAction}>
                {loggedIn ? "Abrir meu painel" : "Entrar com Discord"}<ArrowRight size={17} />
              </button>
            </div>
            <div className="osk-trust-row" aria-label="Vantagens do painel">
              <span><Check size={14} /> Login seguro</span>
              <span><Check size={14} /> Feito para celular</span>
              <span><Check size={14} /> Sincronização automática</span>
            </div>
          </div>
        </section>

        <section id="funcoes" className="osk-public-features osk-public-features--simple">
          <header>
            <span className="osk-kicker">Funções disponíveis</span>
            <h2>Tudo em um só painel.</h2>
            <p>Abra uma função, altere os campos necessários e continue de onde parou.</p>
          </header>
          <div className="osk-feature-grid">
            {features.map((feature) => (
              <article key={feature.title}>
                <span><feature.icon size={20} /></span>
                <div><h3>{feature.title}</h3><p>{feature.text}</p></div>
              </article>
            ))}
          </div>
        </section>

        <section className="osk-public-cta osk-public-cta--simple">
          <div>
            <span className="osk-kicker">Pronto para começar?</span>
            <h2>Escolha seu servidor e configure a Osaka.</h2>
          </div>
          <button className="osk-primary-button" onClick={primaryAction}>{loggedIn ? "Abrir painel" : "Continuar com Discord"}<ArrowRight size={17} /></button>
        </section>
      </main>

      <footer className="osk-public-footer">
        <Brand compact />
        <nav><button onClick={() => onNavigate("/privacy")}>Privacidade</button><button onClick={() => onNavigate("/terms")}>Termos</button></nav>
      </footer>
    </div>
  );
}
