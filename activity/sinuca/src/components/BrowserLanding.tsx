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
  { icon: DoorOpen, title: "Boas-vindas", text: "Monte mensagens, embeds, Components V2, cargos automáticos e webhooks sem editar arquivos." },
  { icon: Ticket, title: "Tickets", text: "Organize atendimento, denúncias, parcerias e sugestões com permissões claras." },
  { icon: ClipboardList, title: "Formulários", text: "Crie perguntas, configure respostas e controle o fluxo de aprovação pela interface." },
  { icon: Palette, title: "Cargos de cor", text: "Gerencie o painel de cores, nomes e cargos sem precisar refazer toda a configuração." },
  { icon: Cake, title: "Aniversários", text: "Defina cadastro, calendário, anúncios, horário e mensagens personalizadas." },
  { icon: Volume2, title: "TTS", text: "Ajuste voz, idioma, velocidade, prefixos, limites e canais de leitura." },
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
  return (
    <div className="osk-public-shell">
      <header className="osk-public-nav">
        <button className="osk-brand-button" onClick={() => onNavigate("/")} aria-label="Página inicial"><Brand /></button>
        <nav aria-label="Navegação principal">
          <a href="#recursos">Recursos</a>
          <button onClick={() => onNavigate("/privacy")}>Privacidade</button>
          <button onClick={() => onNavigate("/terms")}>Termos</button>
        </nav>
        {loggedIn ? (
          <button className="osk-account-button" onClick={onDashboard}>
            <SmartAvatar src={user?.avatarUrl} name={name} type="user" alt={name} size={30} />
            <span>Meu painel</span>
            <ArrowRight size={15} />
          </button>
        ) : (
          <button className="osk-primary-button osk-primary-button--small" onClick={onLogin}>Entrar com Discord</button>
        )}
      </header>

      <main>
        <section className="osk-public-hero">
          <div className="osk-public-hero-copy">
            <span className="osk-kicker"><ShieldCheck size={14} /> Configuração segura para o seu servidor</span>
            <h1>Seu bot organizado.<br /><span>Seu servidor do seu jeito.</span></h1>
            <p>Configure os principais módulos da Osaka em um painel web responsivo, com validações, prévias e alterações sincronizadas com o bot.</p>
            <div className="osk-public-actions">
              <button className="osk-primary-button" onClick={loggedIn ? onDashboard : onLogin}>
                {loggedIn ? "Abrir meu painel" : "Entrar com Discord"}<ArrowRight size={17} />
              </button>
              <a className="osk-secondary-button" href="#recursos">Conhecer os módulos</a>
            </div>
            <div className="osk-trust-row">
              <span><Check size={14} /> Sem tokens no navegador</span>
              <span><Check size={14} /> Permissões verificadas</span>
              <span><Check size={14} /> Feito para celular</span>
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
                  <div className="osk-showcase-heading"><i /><i /></div>
                  <div className="osk-showcase-stats"><i /><i /><i /></div>
                  <div className="osk-showcase-card">
                    <span className="osk-showcase-card-icon"><DoorOpen size={18} /></span>
                    <span><strong>Boas-vindas</strong><small>8 de 10 campos configurados</small></span>
                    <em>Pronto</em>
                  </div>
                  <div className="osk-showcase-card">
                    <span className="osk-showcase-card-icon"><Ticket size={18} /></span>
                    <span><strong>Tickets</strong><small>Equipe e painel conectados</small></span>
                    <em>Ativo</em>
                  </div>
                </section>
              </div>
            </div>
          </div>
        </section>

        <section id="recursos" className="osk-public-features">
          <header>
            <span className="osk-kicker">Módulos disponíveis</span>
            <h2>O que já pode ser configurado no painel</h2>
            <p>A interface mostra somente recursos conectados ao comportamento real do bot.</p>
          </header>
          <div className="osk-feature-grid">
            {features.map((feature) => (
              <article key={feature.title}>
                <span><feature.icon size={20} /></span>
                <h3>{feature.title}</h3>
                <p>{feature.text}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="osk-public-cta">
          <div><span className="osk-kicker">Pronto para configurar?</span><h2>Entre com sua conta do Discord e escolha um servidor.</h2></div>
          <button className="osk-primary-button" onClick={loggedIn ? onDashboard : onLogin}>{loggedIn ? "Abrir painel" : "Continuar com Discord"}<ArrowRight size={17} /></button>
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
