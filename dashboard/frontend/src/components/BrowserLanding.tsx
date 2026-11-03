import { useRef, useState, type CSSProperties } from "react";
import { ArrowDown, ArrowUpRight, AudioLines, Check, Hash, Headphones, LogIn, MessagesSquare, MicOff, SlidersHorizontal, Volume2 } from "lucide-react";
import { MODULE_CATALOG } from "../moduleCatalog";
import type { DashboardSupportServerPayload, DashboardUserPayload } from "../types/dashboard";
import { AccountMenu } from "./AccountMenu";
import { SmartAvatar } from "./SmartAvatar";
import { TtsSamplePlayer } from "./landing/TtsSamplePlayer";
import { useLandingMotion } from "./landing/useLandingMotion";
import { TTS_SAMPLE_TRANSCRIPT } from "./landing/ttsSamples";

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
const botAvatarFallback = "/assets/osaka-discord-avatar.png";
const botNameFallback = "✰ 𝖮𝗌𝖺𝗄𝖺";
const extraIds = ["welcome", "tickets", "forms", "color_roles", "birthday", "economy"];
const extraDescriptions: Record<string, string> = {
  welcome: "Mensagens para receber novos membros.", tickets: "Canais de atendimento separados.",
  forms: "Formulários com perguntas e aprovação.", color_roles: "Cargos de cor para os membros.",
  birthday: "Lembretes de aniversário no servidor.", economy: "Jogos e fichas para jogar com a galera.",
};
const extras = extraIds.map(id => MODULE_CATALOG.find(module => module.id === id)).filter(module => !!module);
const waveHeights = [12, 24, 40, 18, 54, 32, 64, 42, 26, 52, 70, 38, 56, 22, 44, 62, 30, 48, 18, 34, 12];

function displayName(identity: DashboardUserPayload | null, fallback: string) {
  return identity?.global_name?.trim() || identity?.username?.trim() || fallback;
}

// Shared by the server picker, invitation and legal pages too.
export function Brand({ compact = false, bot = null, tagline = "Dashboard", fallbackImage = botAvatarFallback }: {
  compact?: boolean; bot?: DashboardUserPayload | null; tagline?: string; fallbackImage?: string;
}) {
  const botName = displayName(bot, botNameFallback);
  const [fallbackFailed, setFallbackFailed] = useState(false);
  return <span className="osk-brand-lockup" data-compact={compact || undefined}>
    {!bot?.avatarUrl && fallbackImage && !fallbackFailed ?
      <span className="osk-brand-symbol osk-brand-bot-avatar" style={{ width: compact ? 34 : 42, height: compact ? 34 : 42 }}>
        <img className="osk-avatar-img" src={fallbackImage} alt="" loading="eager" onError={() => setFallbackFailed(true)} />
      </span> : <SmartAvatar className="osk-brand-symbol osk-brand-bot-avatar" src={bot?.avatarUrl} fallbackSrc={fallbackFailed ? undefined : fallbackImage}
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

function CharacterPanel({ name }: { name: string }) {
  const [failed, setFailed] = useState(false);
  return <div className="osaka-hero-art" data-hero-art>
    <div className="osaka-art-side-note" aria-hidden="true">{name} / Texto pra voz</div>
    <ComicStar className="osaka-star osaka-star--hero" />
    <figure className="osaka-manga-panel" data-parallax>
      <div className="osaka-panel-topline"><span>Texto → voz</span><AudioLines size={17} /></div>
      <div className="osaka-character-wrap" data-failed={failed || undefined}>
        {!failed && <img src={characterUrl} alt="Ilustração da personagem de braços abertos."
          width={555} height={777} loading="eager" decoding="async" onError={() => setFailed(true)} />}
        {failed && <span className="osaka-character-fallback" aria-hidden="true">お<br />さ<br />か</span>}
      </div>
      <span className="osaka-speech-bubble">tá sem mic?<br /><strong>manda no chat.</strong></span>
      <figcaption><span>No Discord</span><span>↗</span></figcaption>
    </figure>
    <div className="osaka-art-stamp" aria-hidden="true"><Headphones size={18} /><span>do chat<br />pra call.</span></div>
  </div>;
}

function StoryStage({ bot, user }: { bot: DashboardUserPayload | null; user: DashboardUserPayload | null }) {
  const botName = displayName(bot, botNameFallback);
  const userName = displayName(user, "você");
  const botAvatar = bot?.avatarUrl || botAvatarFallback;
  return <div className="osaka-story-stage" data-story-stage aria-hidden="true">
    <div className="osaka-stage-heading"><span>No Discord</span><span>↘</span></div>
    <div className="osaka-stage-grid" />
    <div className="osaka-scene-call" data-scene="call">
      <span className="osaka-scene-label">Canal de voz</span>
      <div className="osaka-channel"><Volume2 size={19} /><strong>geral</strong></div>
      <div className="osaka-voice-member"><SmartAvatar className="osaka-demo-avatar" src={user?.avatarUrl} name={userName} type="user" size={28} alt="" /><span>{userName}</span><MicOff size={15} /></div>
      <div className="osaka-voice-member"><SmartAvatar className="osaka-demo-avatar osaka-demo-avatar--bot" src={botAvatar} fallbackSrc={botAvatarFallback} name={botName} type="user" size={28} alt="" /><span className="osaka-demo-bot-name">{botName} <small>APP</small></span><AudioLines size={17} /></div>
    </div>
    <span className="osaka-scene-connector osaka-scene-connector--one">↓</span>
    <div className="osaka-scene-message" data-scene="message">
      <span className="osaka-scene-label">Mensagem</span>
      <span className="osaka-chat-channel"><Hash size={15} /> conversa</span>
      <div className="osaka-chat-line"><SmartAvatar className="osaka-demo-avatar" src={user?.avatarUrl} name={userName} type="user" size={28} alt="" /><div><strong>{userName} <small>agora</small></strong><p><b>,</b>{TTS_SAMPLE_TRANSCRIPT}</p></div></div>
      <div className="osaka-chat-compose"><span>Enviada</span><Check size={14} /></div>
    </div>
    <span className="osaka-scene-connector osaka-scene-connector--two">↓</span>
    <div className="osaka-scene-voice" data-scene="voice">
      <span className="osaka-scene-label">Leitura na call</span>
      <span className="osaka-voice-quote">“{TTS_SAMPLE_TRANSCRIPT}”</span>
      <div className="osaka-story-wave">{waveHeights.map((height, index) => <i key={index} data-voice-bar
        style={{ "--wave-height": height + "px", "--wave-index": index } as CSSProperties} />)}</div>
      <span className="osaka-scene-footer"><SmartAvatar className="osaka-voice-avatar" src={botAvatar} fallbackSrc={botAvatarFallback} name={botName} type="user" size={22} alt="" /><span className="osaka-demo-bot-name">{botName}</span><AudioLines size={16} /></span>
    </div>
  </div>;
}

export function BrowserLanding({ loggedIn, sessionPending = false, user, bot, supportServer, refreshing = false,
  onLogin, onDashboard, onRefresh, onLogout, onNavigate }: BrowserLandingProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  useLandingMotion(rootRef);
  const botName = displayName(bot, botNameFallback);
  const primaryAction = loggedIn ? onDashboard : onLogin;
  const primaryLabel = sessionPending ? "Verificando acesso…" : loggedIn ? "Abrir dashboard" : "Entrar com Discord";
  const supportInviteUrl = supportServer?.inviteUrl || "https://discord.gg/RckuzJbvVk";

  return <div className="osaka-home" ref={rootRef}>
    <a className="osaka-skip-link" href="#conteudo-osaka">Pular para o conteúdo</a>
    <header className="osaka-header"><div className="osaka-container osaka-header-inner">
      <button type="button" className="osaka-brand-button" onClick={() => onNavigate("/")} aria-label={`${botName}, página inicial`}>
        <Brand bot={bot} tagline="Texto pra voz" fallbackImage={botAvatarFallback} />
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
          <p className="osaka-eyebrow" data-hero-copy><span /> TEXTO PRA VOZ NO DISCORD</p>
          <h1 id="landing-title" data-hero-copy>Você escreve.<br />O bot fala<br /><span className="osaka-call-word">na call.<svg viewBox="0 0 320 16" aria-hidden="true"><path d="M3 11C76 2 198 3 314 8M51 15c93-7 174-8 242-3" /></svg></span></h1>
          <p className="osaka-hero-description" data-hero-copy>Use o chat para conversar na call,<br className="osaka-desktop-break" /> mesmo sem microfone.</p>
          <div className="osaka-hero-actions" data-hero-copy>
            <button type="button" className="osaka-button" onClick={primaryAction} disabled={sessionPending} aria-busy={sessionPending}>{primaryLabel}<ArrowUpRight size={19} aria-hidden="true" /></button>
            <a className="osaka-listen-link" href="#ouvir-osaka"><Headphones size={17} aria-hidden="true" /> Ouvir vozes <ArrowDown size={14} aria-hidden="true" /></a>
          </div>
        </div>
        <CharacterPanel name={botName} />
        <div className="osaka-hero-baseline"><span>VOZES, IDIOMAS E AJUSTES PELO PAINEL.</span><a href="#como-funciona">COMO FUNCIONA <ArrowDown size={13} /></a></div>
      </section>
      <section className="osaka-listening-section" aria-labelledby="osaka-listening-title">
        <div className="osaka-container osaka-listening-layout">
          <div className="osaka-listening-copy"><p className="osaka-eyebrow">VOZES</p>
            <h2 id="osaka-listening-title">Ouça antes<br /><span>de escolher.</span></h2>
            <p>Compare as vozes com a mesma mensagem.</p>
          </div><TtsSamplePlayer />
        </div>
      </section>
      <section id="como-funciona" className="osaka-how-section osaka-container" aria-labelledby="osaka-how-title">
        <div className="osaka-section-heading"><p className="osaka-eyebrow">COMO USAR</p>
          <h2 id="osaka-how-title">Do chat<br />para a call<span className="osaka-heading-dot">.</span></h2><span className="osaka-section-number" aria-hidden="true">01—03</span>
        </div>
        <div className="osaka-story-layout"><div className="osaka-story-steps">
          <span className="osaka-story-track" aria-hidden="true"><i data-story-progress /></span>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">01</span>
            <div><span className="osaka-step-kicker">CANAL DE VOZ</span><h3>Entre em uma call.</h3><p>Entre no canal de voz em que você quer conversar. Seu microfone pode continuar desligado.</p></div>
          </article>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">02</span>
            <div><span className="osaka-step-kicker">CHAT DE TEXTO</span><h3>Envie uma mensagem.</h3><p>Escreva no chat do servidor e coloque o prefixo do TTS antes da mensagem:</p><code className="osaka-example-message">,{TTS_SAMPLE_TRANSCRIPT}</code><small>A vírgula é o padrão. O prefixo pode mudar em cada servidor. Mensagens sem prefixo não são lidas.</small></div>
          </article>
          <article className="osaka-story-step" data-story-step><span className="osaka-step-number">03</span>
            <div><span className="osaka-step-kicker">LEITURA</span><h3>O bot lê pra você.</h3><p>O bot pode entrar automaticamente no canal de voz e ler o texto. Continue escrevendo no chat para conversar com quem está na call.</p><a className="osaka-inline-link" href="#ouvir-osaka">Ouvir as vozes <ArrowUpRight size={15} /></a></div>
          </article>
        </div><StoryStage bot={bot} user={user} /></div>
        <p className="osaka-permissions-note">O TTS precisa estar ativo, e o bot precisa de permissão para conectar e falar no canal.</p>
      </section>
      <section className="osaka-tuning-section" aria-labelledby="osaka-tuning-title">
        <div className="osaka-container osaka-tuning-layout">
          <div className="osaka-tuning-illustration" data-reveal aria-hidden="true"><div className="osaka-tuning-sheet">
            <span className="osaka-tuning-sheet-label"><SlidersHorizontal size={16} /> Preferências de voz</span>
            <div className="osaka-tuning-row"><span>Voz</span><strong>Francisca <span>⌄</span></strong></div>
            <div className="osaka-tuning-row"><span>Idioma</span><strong>Português · Brasil</strong></div>
            <div className="osaka-tuning-slider"><span>Velocidade <b>+20%</b></span><i><b /></i></div>
            <div className="osaka-tuning-slider osaka-tuning-slider--pitch"><span>Tom <b>+0 Hz</b></span><i><b /></i></div>
          </div><ComicStar className="osaka-star osaka-star--tuning" /></div>
          <div className="osaka-tuning-copy"><p className="osaka-eyebrow">PREFERÊNCIAS</p>
            <h2 id="osaka-tuning-title">Escolha a voz.<br /><span>Ajuste como quiser.</span></h2>
            <p>Troque a voz e o idioma ou ajuste a velocidade e o tom das vozes Edge. Você pode salvar suas preferências, e o servidor pode definir um padrão para os membros.</p>
            <p className="osaka-tuning-help">No Discord, <code>/tts menu</code> abre suas preferências. No painel, você configura o TTS do servidor.</p>
            <button type="button" className="osaka-inline-link" onClick={primaryAction} disabled={sessionPending}>{sessionPending ? "Verificando acesso…" : "Abrir o painel"}<ArrowUpRight size={17} /></button>
          </div>
        </div>
      </section>
      <section id="outras-funcoes" className="osaka-extras osaka-container" aria-labelledby="osaka-extras-title">
        <div className="osaka-extras-intro"><p className="osaka-eyebrow">OUTRAS FUNÇÕES</p><h2 id="osaka-extras-title">Além do TTS</h2><p>Boas-vindas, atendimento e jogos<br className="osaka-desktop-break" /> no mesmo bot.</p></div>
        <div className="osaka-extras-list">{extras.map(feature => <article className="osaka-extra" key={feature.id}>
          <feature.icon size={21} strokeWidth={1.5} aria-hidden="true" /><div><h3>{feature.label}</h3><p>{extraDescriptions[feature.id]}</p></div>
        </article>)}</div>
      </section>
      <section className="osaka-closing" aria-labelledby="osaka-closing-title"><div className="osaka-container osaka-closing-inner">
        <ComicStar className="osaka-closing-star" /><p className="osaka-eyebrow">SEU SERVIDOR</p>
        <h2 id="osaka-closing-title">Tudo pronto<br />pra conversar<span>?</span></h2>
        <button type="button" className="osaka-button" onClick={primaryAction} disabled={sessionPending} aria-busy={sessionPending}>{primaryLabel}<ArrowUpRight size={20} /></button>
        <a className="osaka-support-link" href={supportInviteUrl} target="_blank" rel="noreferrer noopener"><MessagesSquare size={16} /> Dúvidas? Entre no servidor de suporte. <ArrowUpRight size={14} /></a>
      </div></section>
    </main>
    <footer className="osaka-footer osaka-container"><div><span className="osaka-footer-name">{botName}</span><p>Texto pra voz no Discord.</p></div>
      <nav aria-label="Informações legais"><button type="button" onClick={() => onNavigate("/terms")}>Termos de uso</button><button type="button" onClick={() => onNavigate("/privacy")}>Privacidade</button><a href={supportInviteUrl} target="_blank" rel="noreferrer noopener">Discord <ArrowUpRight size={12} /></a></nav>
      <span className="osaka-footer-note">Configurações pelo painel do servidor.</span>
    </footer>
  </div>;
}
