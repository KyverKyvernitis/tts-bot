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
  { icon: DoorOpen, title: "Boas-vindas", text: "Mensagens, cargos automáticos e aparência." },
  { icon: Ticket, title: "Tickets", text: "Atendimento, equipe e permissões." },
  { icon: ClipboardList, title: "Formulários", text: "Perguntas, respostas e aprovação." },
  { icon: Palette, title: "Cargos de cor", text: "Painéis e cargos personalizados." },
  { icon: Cake, title: "Aniversários", text: "Cadastro, calendário e anúncios." },
  { icon: Volume2, title: "TTS", text: "Voz, idioma, canais e regras de leitura." },
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
            <SmartAvatar src={user?.avatarUrl} name={name} type="user" alt={name} size={30} />
            <span>Abrir painel</span>
            <ArrowRight size={15} />
          </button>
        ) : (
          <button className="osk-primary-button osk-primary-button--small" onClick={onLogin}>Entrar com Discord</button>
        )}
      </header>

      <main>
        <section className="osk-public-hero osk-public-hero--simple">
          <div className="osk-public-hero-copy">
            <span className="osk-kicker"><ShieldCheck size={14} /> Painel oficial da Osaka</span>
            <h1>Configure seu servidor <span>sem usar comandos.</span></h1>
            <p>Escolha o servidor, abra uma função e salve. A Osaka aplica as mudanças diretamente no bot.</p>
            <div className="osk-public-actions">
              <button className="osk-primary-button" onClick={primaryAction}>
                {loggedIn ? "Abrir meu painel" : "Entrar com Discord"}<ArrowRight size={17} />
              </button>
              <a className="osk-secondary-button" href="#funcoes">Ver funções</a>
            </div>
            <div className="osk-trust-row">
              <span><Check size={14} /> Login seguro pelo Discord</span>
              <span><Check size={14} /> Feito para celular</span>
              <span><Check size={14} /> Alterações sincronizadas</span>
            </div>
          </div>

          <div className="osk-product-showcase" aria-label="Prévia do painel">
            <div className="osk-showcase-glow" />
            <div className="osk-showcase-window">
              <div className="osk-showcase-top">
                <span className="osk-showcase-dots"><i /><i /><i /></span>
                <span>painel.osaka</span>
                <span className="osk-showcase-live">online</span>
              </div>
              <div className="osk-showcase-layout">
                <aside>
                  <Brand compact />
                  {[DoorOpen, Ticket, ClipboardList, Volume2].map((Icon, index) => (
                    <span key={index} data-active={index === 0}><Icon size={15} /><i /></span>
                  ))}
                </aside>
                <section>
                  <div className="osk-showcase-heading">
                    <strong>Funções do servidor</strong>
                    <small>Escolha uma opção para configurar</small>
                  </div>
                  <div className="osk-showcase-list">
                    <ShowcaseItem icon={DoorOpen} title="Boas-vindas" text="Mensagens e cargos" />
                    <ShowcaseItem icon={Ticket} title="Tickets" text="Atendimento e equipe" />
                    <ShowcaseItem icon={Volume2} title="TTS" text="Voz e canais" />
                  </div>
                </section>
              </div>
            </div>
          </div>
        </section>

        <section id="funcoes" className="osk-public-features osk-public-features--simple">
          <header>
            <span className="osk-kicker">Funções disponíveis</span>
            <h2>O essencial, sem complicação.</h2>
            <p>Cada função é opcional e pode ser ajustada separadamente.</p>
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
          <div><span className="osk-kicker">Começar agora</span><h2>Entre com o Discord e escolha um servidor.</h2></div>
          <button className="osk-primary-button" onClick={primaryAction}>{loggedIn ? "Abrir painel" : "Continuar com Discord"}<ArrowRight size={17} /></button>
        </section>
      </main>

      <footer className="osk-public-footer">
        <Brand compact />
        <p>Painel oficial de configuração da Osaka.</p>
        <nav><button onClick={() => onNavigate("/privacy")}>Privacidade</button><button onClick={() => onNavigate("/terms")}>Termos</button></nav>
      </footer>
    </div>
  );
}

function ShowcaseItem({ icon: Icon, title, text }: { icon: typeof DoorOpen; title: string; text: string }) {
  return <div className="osk-showcase-card">
    <span className="osk-showcase-card-icon"><Icon size={18} /></span>
    <span><strong>{title}</strong><small>{text}</small></span>
    <ArrowRight size={15} />
  </div>;
}
