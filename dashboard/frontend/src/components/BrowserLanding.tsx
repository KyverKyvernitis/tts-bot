import { useRef, useState, type CSSProperties } from "react";
import { ArrowDown, ArrowUpRight, AudioLines, Check, Hash, Headphones, LogIn, MessagesSquare, MicOff, SlidersHorizontal, Volume2 } from "lucide-react";
import { MODULE_CATALOG } from "../moduleCatalog";
import type { DashboardSupportServerPayload, DashboardUserPayload } from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { SmartAvatar } from "./SmartAvatar";
import { TtsSamplePlayer } from "./landing/TtsSamplePlayer";
import { useLandingMotion } from "./landing/useLandingMotion";

interface BrowserLandingProps {
  loggedIn: boolean;
  sessionPending?: boolean;
  user: DashboardUserPayload | null;
  bot: DashboardUserPayload | null;
  supportServer: DashboardSupportServerPayload | null;
  refreshing?: boolean;
  onLogin(): void;
  onDashboard(): void;
  onRefresh(): void;
  onLogout(): void;
  onNavigate(path: string): void;
}
const characterUrl = "/assets/osaka-landing-character.jpg";
const extraIds = ["welcome", "tickets", "forms", "color_roles", "birthday", "economy"];
const extraDescriptions: Record<string, string> = {
  welcome: "Uma recepção com a cara do seu servidor.", tickets: "Um espaço para cada atendimento.",
  forms: "Perguntas e respostas organizadas.", color_roles: "Cada pessoa escolhe a sua cor.",
  birthday: "O próximo parabéns já tem data.", economy: "Fichas, jogos e mais assunto na call.",
};
const extras = extraIds.map(id => MODULE_CATALOG.find(module => module.id === id)).filter(module => !!module);
const waveHeights = [12, 24, 40, 18, 54, 32, 64, 42, 26, 52, 70, 38, 56, 22, 44, 62, 30, 48, 18, 34, 12];

function displayName(identity: DashboardUserPayload | null, fallback: string) {
  return identity?.global_name?.trim() || identity?.username?.trim() || fallback;
}

// Shared by the server picker, invitation and legal pages too.
export function Brand({ compact = false, bot = null, tagline = "Dashboard", fallbackImage }: {
  compact?: boolean; bot?: DashboardUserPayload | null; tagline?: string; fallbackImage?: string;
}) {
  const botName = displayName(bot, "Osaka");
  const [fallbackFailed, setFallbackFailed] = useState(false);
  return <span className="osk-brand-lockup" data-compact={compact || undefined}>
    {!bot?.avatarUrl && fallbackImage && !fallbackFailed ?
      <span className="osk-brand-symbol osk-brand-bot-avatar" style={{ width: compact ? 34 : 42, height: compact ? 34 : 42 }}>
        <img className="osk-avatar-img" src={fallbackImage} alt="" loading="eager" onError={() => setFallbackFailed(true)} />
      </span> : <SmartAvatar className="osk-brand-symbol osk-brand-bot-avatar" src={bot?.avatarUrl}
        name={botName} type="user" size={compact ? 34 : 42} alt="" loading="eager" />}
    <span className="osk-brand-copy"><strong>{botName}</strong><small>{tagline}</small></span>
  </span>;
}

function ComicStar({ className = "" }: { className?: string }) {
  return <svg className={className} viewBox="0 0 80 80" fill="none" aria-hidden="true">
    <path d="M40 3 48 29 75 40 48 48 40 77 30 49 4 40 30 30Z" stroke="currentColor" strokeWidth="1.5" />
    <path d="m15 13 8 8m36 37 8 8m-1-52-9 9M22 58l-9 9" stroke="currentColor" />
  </svg>;
}

function CharacterPanel() {
  const [failed, setFailed] = useState(false);
  return <div className="osaka-hero-art" data-hero-art>
    <div className="osaka-art-side-note" aria-hidden="true">OSAKA / TEXTO PRA VOZ</div>
    <ComicStar className="osaka-star osaka-star--hero" />
    <figure className="osaka-manga-panel" data-parallax>
      <div className="osaka-panel-topline"><span>CONEXÃO ESTABELECIDA</span><AudioLines size={17} /></div>
      <div className="osaka-character-wrap" data-failed={failed || undefined}>
        {!failed && <img src={characterUrl} alt="Osaka, a personagem de braços abertos, pronta para conversar."
          width={555} height={777} loading="eager" decoding="async" onError={() => setFailed(true)} />}
        {failed && <span className="osaka-character-fallback" aria-hidden="true">お<br />さ<br />か</span>}
      </div>
      <span className="osaka-speech-bubble">pode deixar.<br /><strong>eu falo por você.</strong></span>
      <figcaption><span>01 / A VOZ DO SEU CHAT</span><span>↗</span></figcaption>
    </figure>
    <div className="osaka-art-stamp" aria-hidden="true"><Headphones size={18} /><span>um texto.<br />uma voz.</span></div>
  </div>;
}

function StoryStage() {
  return <div className="osaka-story-stage" data-story-stage aria-hidden="true">
    <div className="osaka-stage-heading"><span>DO CHAT PRA CALL</span><span>↘</span></div>
    <div className="osaka-stage-grid" />
    <div className="osaka-scene-call" data-scene="call">
      <span className="osaka-scene-label">01 / CHEGUE MAIS</span>
      <div className="osaka-channel"><Volume2 size={19} /><strong>conversa-sem-fim</strong></div>
      <div className="osaka-voice-member"><span className="osaka-demo-avatar">v</span><span>você</span><MicOff size={15} /></div>
      <div className="osaka-voice-member"><span className="osaka-demo-avatar osaka-demo-avatar--bot">o</span><span>Osaka <small>APP</small></span><AudioLines size={17} /></div>
    </div>
    <span className="osaka-scene-connector osaka-scene-connector--one">↓</span>
    <div className="osaka-scene-message" data-scene="message">
      <span className="osaka-scene-label">02 / MANDA NO CHAT</span>
      <span className="osaka-chat-channel"><Hash size={15} /> bate-papo</span>
      <div className="osaka-chat-line"><span className="osaka-demo-avatar">v</span><div><strong>você <small>agora</small></strong><p><b>,</b>oi, gente!</p></div></div>
      <div className="osaka-chat-compose"><span>Mensagem enviada</span><Check size={14} /></div>
    </div>
    <span className="osaka-scene-connector osaka-scene-connector--two">↓</span>
    <div className="osaka-scene-voice" data-scene="voice">
      <span className="osaka-scene-label">03 / O TEXTO GANHA VOZ</span>
      <span className="osaka-voice-quote">“oi, gente!”</span>
      <div className="osaka-story-wave">{waveHeights.map((height, index) => <i key={index} data-voice-bar
        style={{ "--wave-height": height + "px", "--wave-index": index } as CSSProperties} />)}</div>
      <span className="osaka-scene-footer"><AudioLines size={14} /> Osaka · texto pra voz</span>
    </div>
    <span className="osaka-stage-caption">UMA DEMONSTRAÇÃO. O SEU SERVIDOR TEM A PRÓPRIA HISTÓRIA.</span>
  </div>;
}

export function BrowserLanding({ loggedIn, sessionPending = false, user, bot, supportServer, refreshing = false,
  onLogin, onDashboard, onRefresh, onLogout, onNavigate }: BrowserLandingProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  useLandingMotion(rootRef);
  const primaryAction = loggedIn ? onDashboard : onLogin;
  const primaryLabel = sessionPending ? "Verificando acesso…" : loggedIn ? "Abrir dashboard" : "Entrar com Discord";
  const supportInviteUrl = supportServer?.inviteUrl || "https://discord.gg/RckuzJbvVk";

  return <div className="osaka-home" ref={rootRef}>
    <a className="osaka-skip-link" href="#conteudo-osaka">Pular para o conteúdo</a>
    <header className="osaka-header"><div className="osaka-container osaka-header-inner">
      <button type="button" className="osaka-brand-button" onClick={() => onNavigate("/")} aria-label="Osaka, página inicial">
        <Brand bot={bot} tagline="Texto pra voz" fallbackImage={characterUrl} />
      </button>
      <nav className="osaka-nav" aria-label="Navegação principal">
        <a href="#como-funciona">Como funciona</a><a href="#outras-funcoes">Outras funções</a>
        <a href={supportInviteUrl} target="_blank" rel="noreferrer noopener">Suporte <ArrowUpRight size={12} /></a>
      </nav>
      {loggedIn && user ? <AccountMenu user={user} variant="landing" busy={refreshing} supportInviteUrl={supportInviteUrl}
        serversLabel="Meus servidores" onServers={onDashboard} onRefresh={onRefresh} onLogout={onLogout} /> :
        <button type="button" className="osaka-nav-login" onClick={onLogin} disabled={sessionPending} aria-busy={sessionPending}>
          <LogIn size={15} /><span>{sessionPending ? "Verificando…" : "Entrar"}</span>
        </button>}
    </div></header>
    <main id="conteudo-osaka">
      <section className="osaka-hero osaka-container" aria-labelledby="landing-title">
        <div className="osaka-hero-copy">
          <p className="osaka-eyebrow" data-hero-copy><span /> SEU CHAT TEM VOZ.</p>
          <h1 id="landing-title" data-hero-copy>Você escreve.<br />A Osaka fala<br /><span className="osaka-call-word">na call.<svg viewBox="0 0 320 16" aria-hidden="true"><path d="M3 11C76 2 198 3 314 8M51 15c93-7 174-8 242-3" /></svg></span></h1>
          <p className="osaka-hero-description" data-hero-copy>Transforme suas mensagens em voz no Discord.<br className="osaka-desktop-break" /> Escolha a voz. Entre na call. Faça parte da conversa.</p>
          <div className="osaka-hero-actions" data-hero-copy>
            <button type="button" className="osaka-button" onClick={primaryAction} disabled={sessionPending} aria-busy={sessionPending}>{primaryLabel}<ArrowUpRight size={19} aria-hidden="true" /></button>
            <a className="osaka-listen-link" href="#ouvir-osaka"><Headphones size={17} aria-hidden="true" /> Ouvir a Osaka <ArrowDown size={14} aria-hidden="true" /></a>
          </div>
          <p className="osaka-hero-footnote" data-hero-copy><MicOff size={13} aria-hidden="true" /> Microfone desligado. Conversa em dia.</p>
        </div>
        <CharacterPanel />
        <div className="osaka-hero-baseline"><span>UM BOT DE TEXTO PRA VOZ. E UM POUCO MAIS.</span><a href="#como-funciona">ROLE PRA CONHECER <ArrowDown size={13} /></a></div>
      </section>
      <section className="osaka-listening-section" aria-labelledby="osaka-listening-title">
        <div className="osaka-container osaka-listening-layout">
          <div className="osaka-listening-copy"><p className="osaka-eyebrow">PRIMEIRO, DÁ O PLAY.</p>
            <h2 id="osaka-listening-title">Muito prazer.<br /><span>Essa é a minha voz.</span></h2>
            <p>A mesma frase, jeitos diferentes de falar.<br />Ouça um pouquinho do que vai pra sua call.</p>
            <span className="osaka-listening-note"><AudioLines size={16} aria-hidden="true" /> Amostras reais do TTS da Osaka.</span>
          </div><TtsSamplePlayer />
        </div>
      </section>
      <section id="como-funciona" className="osaka-how-section osaka-container" aria-labelledby="osaka-how-title">
        <div className="osaka-section-heading"><p className="osaka-eyebrow">DO SEU TECLADO PROS FONES DA GALERA.</p>
          <h2 id="osaka-how-title">É assim que<br />a conversa acontece<span className="osaka-heading-dot">.</span></h2><span className="osaka-section-number" aria-hidden="true">01—03</span>
        </div>
        <div className="osaka-story-layout"><div className="osaka-story-steps">
          <span className="osaka-story-track" aria-hidden="true"><i data-story-progress /></span>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">01</span>
            <div><span className="osaka-step-kicker">CHEGUE MAIS</span><h3>Entra na call.</h3><p>Pode deixar o microfone desligado. Entre em um canal de voz do seu servidor para a Osaka saber onde falar.</p><span className="osaka-step-aside"><Headphones size={16} /> O seu lugar na conversa está aqui.</span></div>
          </article>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">02</span>
            <div><span className="osaka-step-kicker">MANDA NO CHAT</span><h3>Escreve do seu jeito.</h3><p>No chat do servidor, use o prefixo do TTS antes da mensagem. Com o padrão da Osaka, fica assim:</p><code className="osaka-example-message">,oi, gente!</code><small>O prefixo pode ser personalizado pelo servidor. Mensagens sem prefixo não são lidas.</small></div>
          </article>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">03</span>
            <div><span className="osaka-step-kicker">AGORA É COMIGO</span><h3>Deixa a Osaka falar.</h3><p>Seu texto vira voz na call. A Osaka pode entrar automaticamente para ler e você continua na conversa, uma mensagem de cada vez.</p><a className="osaka-inline-link" href="#ouvir-osaka">Ouvir as vozes <ArrowUpRight size={15} /></a></div>
          </article>
        </div><StoryStage /></div>
        <p className="osaka-permissions-note">Para funcionar, o TTS precisa estar ativo e a Osaka precisa de permissão para conectar e falar no canal.</p>
      </section>
      <section className="osaka-tuning-section" aria-labelledby="osaka-tuning-title">
        <div className="osaka-container osaka-tuning-layout">
          <div className="osaka-tuning-illustration" data-reveal aria-hidden="true"><div className="osaka-tuning-sheet">
            <span className="osaka-tuning-sheet-label"><SlidersHorizontal size={16} /> SEU JEITO DE FALAR</span>
            <div className="osaka-tuning-row"><span>Voz</span><strong>Francisca <span>⌄</span></strong></div>
            <div className="osaka-tuning-row"><span>Idioma</span><strong>Português · Brasil</strong></div>
            <div className="osaka-tuning-slider"><span>Velocidade <b>+20%</b></span><i><b /></i></div>
            <div className="osaka-tuning-slider osaka-tuning-slider--pitch"><span>Tom <b>+0 Hz</b></span><i><b /></i></div>
            <span className="osaka-tuning-sheet-caption">UMA PRÉVIA DAS CONFIGURAÇÕES DO TTS</span>
          </div><ComicStar className="osaka-star osaka-star--tuning" /></div>
          <div className="osaka-tuning-copy"><p className="osaka-eyebrow">A VOZ TAMBÉM É SUA.</p>
            <h2 id="osaka-tuning-title">Uma Osaka.<br />Vários jeitos<br /><span>de soar.</span></h2>
            <p>Escolha vozes e idiomas, ajuste a velocidade e o tom no Edge. Suas preferências acompanham você; o servidor também pode definir os próprios padrões.</p>
            <p className="osaka-tuning-help">No Discord, <code>/tts menu</code> abre suas preferências. No painel, você configura o TTS do servidor.</p>
            <button type="button" className="osaka-inline-link" onClick={primaryAction} disabled={sessionPending}>{sessionPending ? "Verificando acesso…" : "Abrir o painel"}<ArrowUpRight size={17} /></button>
          </div>
        </div>
      </section>
      <section id="outras-funcoes" className="osaka-extras osaka-container" aria-labelledby="osaka-extras-title">
        <div className="osaka-extras-intro"><p className="osaka-eyebrow">ALÉM DA VOZ.</p><h2 id="osaka-extras-title">Já que eu tô aqui…</h2><p>O TTS é o meu forte. Mas também dou uma mão<br className="osaka-desktop-break" /> no resto do servidor.</p></div>
        <div className="osaka-extras-list">{extras.map(feature => <article className="osaka-extra" key={feature.id}>
          <feature.icon size={21} strokeWidth={1.5} aria-hidden="true" /><div><h3>{feature.label}</h3><p>{extraDescriptions[feature.id]}</p></div>
        </article>)}</div>
      </section>
      <section className="osaka-closing" aria-labelledby="osaka-closing-title"><div className="osaka-container osaka-closing-inner">
        <ComicStar className="osaka-closing-star" /><p className="osaka-eyebrow">O PRÓXIMO “OI” PODE SER SEU.</p>
        <h2 id="osaka-closing-title">A gente se ouve<br />na call<span>?</span></h2>
        <button type="button" className="osaka-button" onClick={primaryAction} disabled={sessionPending} aria-busy={sessionPending}>{primaryLabel}<ArrowUpRight size={20} /></button>
        <a className="osaka-support-link" href={supportInviteUrl} target="_blank" rel="noreferrer noopener"><MessagesSquare size={16} /> Precisa de uma mão? Entre no suporte. <ArrowUpRight size={14} /></a>
      </div></section>
    </main>
    <footer className="osaka-footer osaka-container"><div><span className="osaka-footer-name">Osaka<ComicStar className="osaka-footer-star" /></span><p>Seu chat tem voz.</p></div>
      <nav aria-label="Informações legais"><button type="button" onClick={() => onNavigate("/terms")}>Termos de uso</button><button type="button" onClick={() => onNavigate("/privacy")}>Privacidade</button><a href={supportInviteUrl} target="_blank" rel="noreferrer noopener">Discord <ArrowUpRight size={12} /></a></nav>
      <span className="osaka-footer-note">Feita pra fazer parte da conversa.</span>
    </footer>
  </div>;
}
