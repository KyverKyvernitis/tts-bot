import { useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Cpu,
  DoorOpen,
  HardDrive,
  HelpCircle,
  Home,
  LayoutGrid,
  Mic,
  Music,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  Ticket,
  Cake,
  UploadCloud,
  Zap,
  type LucideIcon,
} from "lucide-react";
import {
  authenticateDiscordAccessToken,
  authorizeDiscordCode,
  bootstrapDiscord,
  clearCachedToken,
  getDiscordSdk,
  getOAuthRedirectUri,
  readCachedToken,
  writeCachedToken,
  writeCachedUser,
} from "./sdk/discord";
import type { ActivityBootstrap } from "./types/activity";
import type { DashboardFieldDefinition, DashboardSectionDefinition, DashboardSectionSummary, DashboardServerCard, DashboardUserPayload } from "./types/dashboard";
import { exchangeDiscordTokenRequest } from "./transport/sessionApi";
import {
  fetchDashboardBootstrap,
  fetchDashboardInvite,
  fetchDashboardServers,
  fetchDashboardSession,
  fetchDashboardSettings,
  fetchDashboardSummary,
  patchDashboardSettings,
} from "./transport/dashboardApi";

const pendingBootstrap: ActivityBootstrap = {
  sdkReady: false,
  clientId: null,
  context: { mode: "casual", instanceId: null, guildId: null, channelId: null, source: "fallback" },
  currentUser: { userId: "pending-auth", displayName: "Carregando usuário...", avatarUrl: null },
  bootDebug: [],
};

function isSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{15,25}$/.test(value);
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(value) : "";
  return String(value);
}

function displayValue(field: DashboardFieldDefinition, value: unknown): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if ((field.type === "channel" || field.type === "role") && Number(value || 0) > 0) {
    return field.type === "channel" ? `<#${value}>` : `<@&${value}>`;
  }
  if (field.type === "select") {
    const raw = stringifyValue(value);
    return field.options?.find((item) => item.value === raw)?.label ?? raw;
  }
  const text = stringifyValue(value).trim();
  return text || "Não configurado";
}

function normalizeInputValue(field: DashboardFieldDefinition, raw: string | boolean): unknown {
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "number") return Number(raw || 0);
  if (field.type === "channel" || field.type === "role") {
    const match = String(raw || "").match(/\d{15,25}/);
    return match ? match[0] : "";
  }
  return raw;
}

function sectionConfiguredLabel(section: DashboardSectionSummary | undefined) {
  if (!section) return "0/0";
  const enabled = section.enabled === null ? "" : section.enabled ? " · ativo" : " · off";
  return `${section.configured}/${section.total}${enabled}`;
}

function cleanErrorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (!raw.trim()) return "Não consegui abrir o dashboard.";
  if (raw.includes("<!doctype") || raw.includes("<html") || raw.includes("html_frontend")) {
    return "A API do dashboard ainda está respondendo com HTML do frontend. Atualize backend/proxy e tente reautenticar.";
  }
  if (raw.includes("api_proxy_returning_frontend_html")) {
    return "A autenticação caiu no frontend em vez do backend. O patch adiciona rotas /token e /api/token para corrigir isso.";
  }
  return raw.replace(/\s+/g, " ").slice(0, 260);
}

function statusClass(summary: DashboardSectionSummary | undefined) {
  if (!summary) return "neutral";
  if (summary.enabled === false) return "off";
  if (summary.configured <= 0) return "pending";
  if (summary.configured >= summary.total) return "ready";
  return "partial";
}

function sectionPercent(summary: DashboardSectionSummary | undefined): number {
  if (!summary || summary.total <= 0) return 0;
  return Math.round((summary.configured / summary.total) * 100);
}

function moduleHint(summary: DashboardSectionSummary): string {
  if (summary.enabled === false) return "Desativado";
  if (summary.total <= 0) return summary.status || "Configurar";
  if (summary.configured >= summary.total) return "Pronto";
  if (summary.configured > 0) return "Parcial";
  return "Configurar";
}


type ModuleGroup = "main" | "system";

type ModuleVisualMeta = {
  id: string;
  label: string;
  description: string;
  emoji: string;
  icon: LucideIcon;
  group: ModuleGroup;
  aliases?: string[];
};

type DashboardVisualModule = DashboardSectionSummary & {
  icon: LucideIcon;
  visualDescription: string;
  group: ModuleGroup;
  available: boolean;
};

const MODULE_CATALOG: ModuleVisualMeta[] = [
  { id: "welcome", label: "Boas-vindas", description: "Mensagem, canal e cargo automático para novos membros.", emoji: "👋", icon: DoorOpen, group: "main" },
  { id: "tickets", label: "Tickets", description: "Categorias, mensagens e permissões de atendimento.", emoji: "🎫", icon: Ticket, group: "main", aliases: ["ticket"] },
  { id: "birthday", label: "Aniversários", description: "Canal de parabéns e cargo do dia.", emoji: "🎂", icon: Cake, group: "main", aliases: ["birthdays"] },
  { id: "music", label: "Música", description: "Filas, controle de DJ e qualidade de áudio.", emoji: "🎵", icon: Music, group: "main" },
  { id: "tts", label: "TTS", description: "Vozes, idiomas e canais de leitura automática.", emoji: "🎙️", icon: Mic, group: "main" },
  { id: "workers", label: "Workers", description: "Processos em segundo plano e filas de tarefas.", emoji: "🧩", icon: Cpu, group: "main", aliases: ["worker", "jobs", "queue"] },
  { id: "updates", label: "Updates", description: "Notas de versão e avisos de atualização do bot.", emoji: "📦", icon: UploadCloud, group: "main", aliases: ["update", "changelog", "releases"] },
  { id: "vps", label: "VPS", description: "Recursos do servidor, status e monitoramento da máquina.", emoji: "🖥️", icon: HardDrive, group: "main", aliases: ["server_host", "host", "machine"] },
  { id: "general", label: "Configurações", description: "Preferências básicas do servidor e do painel.", emoji: "⚙️", icon: Settings, group: "system", aliases: ["guild", "config", "settings", "configuracoes"] },
];

function normalizeModuleId(id: string): string {
  return id.replace(/-/g, "_").toLowerCase();
}

function findModuleMeta(sectionId: string | null | undefined): ModuleVisualMeta | undefined {
  const normalized = normalizeModuleId(sectionId || "");
  return MODULE_CATALOG.find((item) => item.id === normalized || item.aliases?.some((alias) => normalizeModuleId(alias) === normalized));
}

function mergeDashboardModules(summary: DashboardSectionSummary[]): DashboardVisualModule[] {
  const byNormalizedId = new Map(summary.map((item) => [normalizeModuleId(item.id), item]));
  const used = new Set<string>();
  const catalogModules = MODULE_CATALOG
    .filter((item) => item.id !== "callkeeper" && item.id !== "call_keeper")
    .map((meta) => {
      const found = byNormalizedId.get(meta.id)
        ?? meta.aliases?.map((alias) => byNormalizedId.get(normalizeModuleId(alias))).find(Boolean)
        ?? null;
      if (found) used.add(normalizeModuleId(found.id));
      return {
        id: found?.id ?? meta.id,
        label: found?.label ?? meta.label,
        emoji: found?.emoji ?? meta.emoji,
        description: found?.description ?? meta.description,
        enabled: found?.enabled ?? null,
        configured: found?.configured ?? 0,
        total: found?.total ?? 0,
        status: found?.status ?? "Configurar",
        icon: meta.icon,
        visualDescription: meta.description,
        group: meta.group,
        available: Boolean(found),
      } satisfies DashboardVisualModule;
    });

  const extraModules = summary
    .filter((item) => !used.has(normalizeModuleId(item.id)) && !normalizeModuleId(item.id).includes("callkeeper") && !normalizeModuleId(item.id).includes("call_keeper"))
    .map((item) => ({
      ...item,
      icon: findModuleMeta(item.id)?.icon ?? LayoutGrid,
      visualDescription: item.description,
      group: findModuleMeta(item.id)?.group ?? "main",
      available: true,
    } satisfies DashboardVisualModule));

  return [...catalogModules, ...extraModules];
}

function sectionShortStatus(summary: DashboardSectionSummary | undefined, available = true): string {
  if (!available) return "Configurar";
  if (!summary) return "Carregando";
  if (summary.enabled === false) return "Desativado";
  if (summary.total <= 0) return summary.status || "Configurar";
  if (summary.configured >= summary.total) return "Pronto";
  if (summary.configured > 0) return `${summary.configured}/${summary.total}`;
  return "Configurar";
}

function moduleDescription(module: DashboardVisualModule): string {
  return module.visualDescription || module.description || "Configurações do módulo.";
}

function buildGuildAvatarText(guildId: string | null | undefined, fallback: string) {
  const label = fallback?.trim() || guildId || "Servidor";
  return guildInitials(label);
}

type RuntimeMode = "detecting" | "activity" | "browser";
type BrowserView = "landing" | "servers" | "invite";

function readBrowserGuildFromLocation(): string | null {
  if (typeof window === "undefined") return null;
  const search = new URLSearchParams(window.location.search);
  const queryGuild = search.get("guild_id") ?? search.get("guildId");
  if (isSnowflake(queryGuild)) return queryGuild;
  const match = window.location.pathname.match(/\/dashboard\/(\d{15,25})/);
  return isSnowflake(match?.[1]) ? match[1] : null;
}

function initialBrowserView(): BrowserView {
  if (typeof window === "undefined") return "landing";
  if (window.location.pathname.startsWith("/dashboard")) return "servers";
  return "landing";
}

function cleanOAuthCodeFromUrl(nextPath?: string) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.delete("code");
  url.searchParams.delete("state");
  url.searchParams.delete("guild_id");
  url.searchParams.delete("guildId");
  const path = nextPath || `${url.pathname}${url.search}${url.hash}`;
  window.history.replaceState({}, "", path);
}

function browserUserName(user: DashboardUserPayload | null, fallback: string) {
  return user?.global_name || user?.username || fallback;
}

function browserUserAvatar(user: DashboardUserPayload | null): string | null {
  if (!user?.id) return null;
  if (user.avatar) return `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png?size=128`;
  try {
    const index = Number((BigInt(user.id) >> 22n) % 6n);
    return `https://cdn.discordapp.com/embed/avatars/${index}.png`;
  } catch {
    return null;
  }
}

function guildInitials(name: string): string {
  return name.split(/\s+/).map((part) => part[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() || "S";
}

function buildBrowserLoginUrl() {
  const clientId = (import.meta.env.VITE_DISCORD_CLIENT_ID as string | undefined)?.trim();
  if (!clientId) return null;
  const redirectUri = getOAuthRedirectUri();
  if (!redirectUri) return null;
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: "identify guilds guilds.members.read",
    prompt: "consent",
    state: "dashboard-browser-login",
  });
  return `https://discord.com/oauth2/authorize?${params.toString()}`;
}

function BrowserTopbar({ loggedIn, user, onLogin, onDashboard }: { loggedIn: boolean; user: DashboardUserPayload | null; onLogin(): void; onDashboard(): void }) {
  const avatar = browserUserAvatar(user);
  return (
    <header className="browser-topbar">
      <button className="browser-brand" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
        <span>⚙️</span>
        <strong>Dashboard</strong>
      </button>
      <button className="browser-login-button" onClick={loggedIn ? onDashboard : onLogin}>
        {loggedIn ? (
          <>
            {avatar ? <img src={avatar} alt="" /> : <span className="browser-avatar-fallback">✓</span>}
            <span>Dashboard</span>
          </>
        ) : (
          <>
            <span>Entrar com Discord</span>
          </>
        )}
      </button>
    </header>
  );
}

function BrowserLanding({ loggedIn, user, busy, message, onLogin, onDashboard }: { loggedIn: boolean; user: DashboardUserPayload | null; busy: boolean; message: string; onLogin(): void; onDashboard(): void }) {
  return (
    <div className="browser-page browser-page--landing">
      <BrowserTopbar loggedIn={loggedIn} user={user} onLogin={onLogin} onDashboard={onDashboard} />
      <section className="browser-hero reveal-card">
        <div className="browser-hero-copy">
          <p className="eyebrow">Dashboard web + Discord Activity</p>
          <h1>Configure o bot sem decorar comandos.</h1>
          <p>Use o painel para ajustar tickets, boas-vindas, TTS, música, logs, permissões e automações do servidor com uma interface visual.</p>
          <div className="browser-hero-actions">
            <button className="primary-button" disabled={busy} onClick={loggedIn ? onDashboard : onLogin}>{loggedIn ? "Abrir Dashboard" : "Entrar com Discord"}</button>
            <a className="ghost-link" href="#guia">Ver guia</a>
          </div>
          {message && <span className="browser-status-line">{message}</span>}
        </div>
        <div className="browser-hero-preview" aria-hidden="true">
          <div className="preview-window">
            <div className="preview-sidebar"><span /><span /><span /></div>
            <div className="preview-main">
              <span className="preview-pill" />
              <span className="preview-card preview-card--wide" />
              <div className="preview-grid"><span /><span /><span /><span /></div>
            </div>
          </div>
        </div>
      </section>

      <section id="guia" className="browser-guide-grid">
        {[
          ["🎫", "Tickets", "Crie painéis, fluxos, cargos staff, mensagens e permissões de atendimento."],
          ["👋", "Boas-vindas", "Configure canal, embed, mensagem, webhook, preview e limpeza automática."],
          ["🔊", "TTS", "Ajuste engine, prefixos, limites, canal padrão e comportamento de voz."],
          ["🎵", "Música", "Defina canal, cargo DJ, volume e preferências do player."],
          ["📜", "Logs", "Centralize canais de auditoria, update, erro, tickets e TTS."],
          ["🛡️", "Permissões", "Somente donos, admins e staff autorizado conseguem alterar o servidor."],
        ].map(([emoji, title, text], index) => (
          <article className="browser-guide-card reveal-card" style={{ transitionDelay: `${index * 45}ms` }} key={title}>
            <span>{emoji}</span>
            <h2>{title}</h2>
            <p>{text}</p>
          </article>
        ))}
      </section>

      <section className="browser-flow-section reveal-card">
        <p className="eyebrow">Como funciona</p>
        <h2>Uma experiência diferente em cada lugar.</h2>
        <div className="browser-flow-grid">
          <article><strong>Dentro do Discord</strong><span>Abre direto no servidor atual após autorização.</span></article>
          <article><strong>No navegador</strong><span>Mostra este guia, login e seleção dos servidores configuráveis.</span></article>
          <article><strong>Sem o bot</strong><span>Servidores aparecem acinzentados e levam para a tela de convite.</span></article>
        </div>
      </section>
    </div>
  );
}

function ServerAvatar({ server }: { server: DashboardServerCard }) {
  return server.icon ? <img src={server.icon} alt="" /> : <span>{guildInitials(server.name)}</span>;
}

function BrowserServerPicker({
  user,
  manageable,
  needsInvite,
  loading,
  message,
  onSelect,
  onInvite,
  onRefresh,
  onLogout,
}: {
  user: DashboardUserPayload | null;
  manageable: DashboardServerCard[];
  needsInvite: DashboardServerCard[];
  loading: boolean;
  message: string;
  onSelect(server: DashboardServerCard): void;
  onInvite(server: DashboardServerCard): void;
  onRefresh(): void;
  onLogout(): void;
}) {
  return (
    <div className="browser-page browser-page--servers">
      <BrowserTopbar loggedIn user={user} onLogin={onRefresh} onDashboard={onRefresh} />
      <section className="server-picker-head reveal-card">
        <div>
          <p className="eyebrow">Servidores</p>
          <h1>Escolha onde configurar.</h1>
          <p>{browserUserName(user, "Sua conta")} pode configurar os servidores ativos abaixo. Servidores sem o bot aparecem desativados para convite.</p>
        </div>
        <div className="button-row">
          <button className="ghost-button" disabled={loading} onClick={onRefresh}>{loading ? "Atualizando..." : "Atualizar"}</button>
          <button className="ghost-button" onClick={onLogout}>Sair</button>
        </div>
        {message && <span className="browser-status-line">{message}</span>}
      </section>

      <section className="server-section reveal-card">
        <div className="panel-title-row"><h2>Com bot instalado</h2><span className="mini-badge mini-badge--ready">{manageable.length}</span></div>
        <div className="server-grid">
          {manageable.map((server) => (
            <button className="server-card server-card--active" key={server.id} onClick={() => onSelect(server)}>
              <span className="server-avatar"><ServerAvatar server={server} /></span>
              <span><strong>{server.name}</strong><small>{server.owner ? "Dono do servidor" : "Staff autorizado"}</small></span>
              <em>Configurar</em>
            </button>
          ))}
          {!loading && manageable.length === 0 && <p className="muted-text">Nenhum servidor configurável com o bot instalado foi encontrado.</p>}
        </div>
      </section>

      <section className="server-section server-section--muted reveal-card">
        <div className="panel-title-row"><h2>Seus servidores sem o bot</h2><span className="mini-badge">{needsInvite.length}</span></div>
        <div className="server-grid">
          {needsInvite.map((server) => (
            <button className="server-card server-card--disabled" key={server.id} onClick={() => onInvite(server)}>
              <span className="server-avatar"><ServerAvatar server={server} /></span>
              <span><strong>{server.name}</strong><small>Bot ainda não está neste servidor</small></span>
              <em>Convidar</em>
            </button>
          ))}
          {!loading && needsInvite.length === 0 && <p className="muted-text">Nenhum servidor pendente de convite.</p>}
        </div>
      </section>
    </div>
  );
}

function BrowserInviteScreen({ server, busy, message, onBack, onOpenInvite }: { server: DashboardServerCard | null; busy: boolean; message: string; onBack(): void; onOpenInvite(): void }) {
  return (
    <div className="browser-page browser-page--invite">
      <section className="invite-panel reveal-card reveal-card--visible">
        <span className="invite-orb">🤖</span>
        <p className="eyebrow">Convidar bot</p>
        <h1>{server ? server.name : "Servidor"}</h1>
        <p>Para configurar este servidor pelo Dashboard, primeiro adicione o bot com as permissões necessárias.</p>
        {message && <span className="browser-status-line">{message}</span>}
        <div className="button-row">
          <button className="ghost-button" onClick={onBack}>Voltar</button>
          <button className="primary-button" disabled={busy || !server} onClick={onOpenInvite}>{busy ? "Preparando..." : "Convidar bot"}</button>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<ActivityBootstrap>(pendingBootstrap);
  const [token, setToken] = useState<string | null>(() => readCachedToken());
  const [authState, setAuthState] = useState<"booting" | "needs_login" | "ready" | "denied" | "error">("booting");
  const [message, setMessage] = useState<string>("Abrindo Dashboard...");
  const [sections, setSections] = useState<DashboardSectionDefinition[]>([]);
  const [summary, setSummary] = useState<DashboardSectionSummary[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [selectedSectionId, setSelectedSectionId] = useState<string>("general");
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mobileView, setMobileView] = useState<"home" | "section">("home");
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>("detecting");
  const [browserView, setBrowserView] = useState<BrowserView>(() => initialBrowserView());
  const [browserUser, setBrowserUser] = useState<DashboardUserPayload | null>(null);
  const [browserManageableServers, setBrowserManageableServers] = useState<DashboardServerCard[]>([]);
  const [browserInviteServers, setBrowserInviteServers] = useState<DashboardServerCard[]>([]);
  const [browserSelectedGuildId, setBrowserSelectedGuildId] = useState<string | null>(() => readBrowserGuildFromLocation());
  const [browserInviteServer, setBrowserInviteServer] = useState<DashboardServerCard | null>(null);
  const [loadingServers, setLoadingServers] = useState(false);

  const activityGuildId = bootstrap.context.guildId;
  const guildId = runtimeMode === "browser" ? browserSelectedGuildId : activityGuildId;
  const selectedSection = useMemo(
    () => sections.find((section) => section.id === selectedSectionId) ?? sections[0] ?? null,
    [sections, selectedSectionId],
  );
  const selectedSummary = useMemo(
    () => summary.find((item) => item.id === selectedSection?.id),
    [summary, selectedSection?.id],
  );

  const changedFields = useMemo(() => {
    if (!selectedSection) return [];
    return selectedSection.fields.filter((field) => draft[field.id] !== values[field.id]);
  }, [draft, selectedSection, values]);


  const dashboardStats = useMemo(() => {
    const totalFields = summary.reduce((acc, item) => acc + item.total, 0);
    const configured = summary.reduce((acc, item) => acc + item.configured, 0);
    const configuredSections = summary.filter((item) => item.total > 0 && item.configured >= item.total).length;
    const percent = totalFields > 0 ? Math.round((configured / totalFields) * 100) : 0;
    return { totalFields, configured, configuredSections, percent };
  }, [summary]);

  async function login(prompt: "none" | "consent" = "consent") {
    const discord = getDiscordSdk();
    if (!discord) {
      setAuthState("error");
      setMessage("SDK do Discord não está disponível nesta janela.");
      return;
    }
    setBusy(true);
    setMessage("Autorizando sua conta...");
    try {
      const auth = await authorizeDiscordCode(prompt);
      if (!auth.code) {
        setAuthState("needs_login");
        setMessage("Autorize sua conta para abrir o dashboard administrativo.");
        return;
      }
      const exchanged = await exchangeDiscordTokenRequest(auth.code, getOAuthRedirectUri());
      if (!exchanged.ok || !exchanged.accessToken) {
        setAuthState("error");
        setMessage(cleanErrorText(exchanged.error || exchanged.detail || "erro desconhecido"));
        return;
      }
      writeCachedToken(exchanged.accessToken);
      const user = await authenticateDiscordAccessToken(discord, exchanged.accessToken, guildId);
      if (user) writeCachedUser(user);
      setToken(exchanged.accessToken);
      setAuthState("ready");
      setMessage("Autorização concluída.");
    } catch (error) {
      setAuthState("error");
      setMessage(cleanErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  function startBrowserLogin() {
    const url = buildBrowserLoginUrl();
    if (!url) {
      setAuthState("error");
      setMessage("Login web não configurado: defina VITE_DISCORD_CLIENT_ID e Redirect URI.");
      return;
    }
    window.location.href = url;
  }

  async function hydrateBrowserSession(accessToken: string) {
    try {
      const session = await fetchDashboardSession(accessToken);
      if (session.ok && session.user) {
        setBrowserUser(session.user);
        setAuthState("ready");
        setMessage("Sessão web conectada.");
        return true;
      }
    } catch {
      // handled below by clearing stale token
    }
    clearCachedToken();
    setToken(null);
    setBrowserUser(null);
    setAuthState("needs_login");
    setMessage("Entre com Discord para ver seus servidores.");
    return false;
  }

  async function finishBrowserOAuthIfNeeded(): Promise<boolean> {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code) return false;
    setBusy(true);
    setMessage("Conectando sua conta Discord...");
    try {
      const exchanged = await exchangeDiscordTokenRequest(code, getOAuthRedirectUri());
      if (!exchanged.ok || !exchanged.accessToken) {
        setAuthState("error");
        setMessage(cleanErrorText(exchanged.error || exchanged.detail || "login_web_failed"));
        cleanOAuthCodeFromUrl("/");
        return true;
      }
      writeCachedToken(exchanged.accessToken);
      setToken(exchanged.accessToken);
      await hydrateBrowserSession(exchanged.accessToken);
      setBrowserView("servers");
      cleanOAuthCodeFromUrl("/dashboard");
      return true;
    } catch (error) {
      setAuthState("error");
      setMessage(cleanErrorText(error));
      cleanOAuthCodeFromUrl("/");
      return true;
    } finally {
      setBusy(false);
    }
  }

  async function loadBrowserServers() {
    if (!token) {
      setBrowserView("landing");
      setAuthState("needs_login");
      setMessage("Entre com Discord para abrir seus servidores.");
      return;
    }
    setLoadingServers(true);
    setMessage("Carregando servidores...");
    try {
      const payload = await fetchDashboardServers(token);
      if (!payload.ok) throw new Error(payload.error || "servers_failed");
      setBrowserUser(payload.user ?? browserUser);
      setBrowserManageableServers(payload.manageable || []);
      setBrowserInviteServers(payload.needsInvite || []);
      setAuthState("ready");
      setMessage("Escolha um servidor para configurar.");
    } catch (error) {
      const text = cleanErrorText(error);
      if (text.includes("401") || text.includes("session_invalid") || text.includes("user_fetch_failed")) {
        clearCachedToken();
        setToken(null);
        setBrowserUser(null);
        setBrowserView("landing");
        setAuthState("needs_login");
        setMessage("Sessão expirada. Entre novamente.");
      } else {
        setAuthState("error");
        setMessage(text || "Não consegui carregar seus servidores.");
      }
    } finally {
      setLoadingServers(false);
    }
  }

  function openBrowserServer(server: DashboardServerCard) {
    setBrowserSelectedGuildId(server.id);
    setBrowserView("servers");
    setMobileView("home");
    setSections([]);
    setSummary([]);
    setValues({});
    setDraft({});
    setMessage(`Abrindo ${server.name}...`);
    window.history.pushState({}, "", `/dashboard/${server.id}`);
  }

  function openBrowserInvite(server: DashboardServerCard) {
    setBrowserInviteServer(server);
    setBrowserView("invite");
    setMessage("Convide o bot para liberar a configuração deste servidor.");
    window.history.pushState({}, "", "/dashboard/invite");
  }

  async function openInviteUrl() {
    if (!browserInviteServer || !token) return;
    setBusy(true);
    try {
      const payload = await fetchDashboardInvite(token, browserInviteServer.id);
      const url = payload.invite_url || browserInviteServer.inviteUrl;
      if (!payload.ok || !url) throw new Error(payload.error || "invite_url_missing");
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (error) {
      const fallback = browserInviteServer.inviteUrl;
      if (fallback) window.open(fallback, "_blank", "noopener,noreferrer");
      else setMessage(cleanErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  function logoutBrowser() {
    clearCachedToken();
    setToken(null);
    setBrowserUser(null);
    setBrowserManageableServers([]);
    setBrowserInviteServers([]);
    setBrowserSelectedGuildId(null);
    setBrowserInviteServer(null);
    setBrowserView("landing");
    setAuthState("needs_login");
    setMessage("Sessão encerrada.");
    window.history.pushState({}, "", "/");
  }

  async function loadDashboard(accessToken: string, targetGuildId: string) {
    setBusy(true);
    setMessage("Carregando configurações do servidor...");
    try {
      const [bootPayload, summaryPayload, settingsPayload] = await Promise.all([
        fetchDashboardBootstrap(accessToken, targetGuildId),
        fetchDashboardSummary(accessToken, targetGuildId),
        fetchDashboardSettings(accessToken, targetGuildId),
      ]);
      if (!bootPayload.ok || !summaryPayload.ok || !settingsPayload.ok) {
        throw new Error(bootPayload.error || summaryPayload.error || settingsPayload.error || "dashboard_load_failed");
      }
      setSections(settingsPayload.sections);
      setSummary(summaryPayload.sections);
      setValues(settingsPayload.values || {});
      setDraft(settingsPayload.values || {});
      if (!settingsPayload.sections.some((section) => section.id === selectedSectionId) && settingsPayload.sections[0]) {
        setSelectedSectionId(settingsPayload.sections[0].id);
      }
      setAuthState("ready");
      setMessage("");
    } catch (error) {
      const text = cleanErrorText(error);
      if (text.includes("403") || text.includes("access_denied") || text.includes("missing_manage_guild")) {
        setAuthState("denied");
        setMessage("Você precisa ser dono, administrador ou ter Gerenciar servidor para configurar este servidor.");
      } else if (text.includes("401") || text.includes("missing_access_token") || text.includes("user_fetch_failed")) {
        clearCachedToken();
        setToken(null);
        setAuthState("needs_login");
        setMessage("Sessão expirada. Autorize novamente para continuar.");
      } else {
        setAuthState("error");
        setMessage(text || "Não consegui carregar o dashboard.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function refreshDashboard() {
    if (!token || !guildId) return;
    await loadDashboard(token, guildId);
  }

  async function saveSection() {
    if (!token || !guildId || !selectedSection) return;
    const updates: Record<string, unknown> = {};
    for (const field of selectedSection.fields) {
      if (draft[field.id] !== values[field.id]) updates[field.id] = draft[field.id];
    }
    if (!Object.keys(updates).length) {
      setMessage("Nada para salvar nesta seção.");
      return;
    }
    setSaving(true);
    setMessage("Salvando alterações...");
    try {
      const result = await patchDashboardSettings(token, guildId, updates);
      setValues(result.values);
      setDraft(result.values);
      setMessage(`Alterações salvas: ${result.saved.length}.`);
      const summaryPayload = await fetchDashboardSummary(token, guildId);
      if (summaryPayload.ok) setSummary(summaryPayload.sections);
    } catch (error) {
      setMessage(cleanErrorText(error));
    } finally {
      setSaving(false);
    }
  }

  function updateDraft(field: DashboardFieldDefinition, raw: string | boolean) {
    setDraft((current) => ({ ...current, [field.id]: normalizeInputValue(field, raw) }));
  }

  function openSection(sectionId: string) {
    setSelectedSectionId(sectionId);
    setMobileView("section");
  }

  useEffect(() => {
    let cancelled = false;
    bootstrapDiscord().then(async (result) => {
      if (cancelled) return;
      setBootstrap(result);

      if (!result.sdkReady) {
        setRuntimeMode("browser");
        const handledOAuth = await finishBrowserOAuthIfNeeded();
        if (cancelled) return;
        if (handledOAuth) return;

        const cachedToken = readCachedToken();
        if (cachedToken) {
          setToken(cachedToken);
          await hydrateBrowserSession(cachedToken);
          if (readBrowserGuildFromLocation()) setBrowserSelectedGuildId(readBrowserGuildFromLocation());
          return;
        }

        setAuthState("needs_login");
        setMessage("Entre com Discord para abrir o guia ou seus servidores.");
        return;
      }

      setRuntimeMode("activity");
      if (!result.context.guildId) {
        setAuthState("error");
        setMessage("Abra o dashboard dentro de um servidor para configurar o bot.");
        return;
      }
      if (!readCachedToken()) {
        setAuthState("needs_login");
        setMessage("Autorize sua conta para continuar.");
      }
    }).catch((error) => {
      if (cancelled) return;
      setRuntimeMode("browser");
      setAuthState("needs_login");
      setMessage(cleanErrorText(error));
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!token || !isSnowflake(guildId)) return;
    void loadDashboard(token, guildId);
  }, [token, guildId, runtimeMode]);

  useEffect(() => {
    if (runtimeMode !== "browser" || !token || browserView !== "servers" || browserSelectedGuildId) return;
    void loadBrowserServers();
  }, [runtimeMode, token, browserView, browserSelectedGuildId]);

  useEffect(() => {
    if (runtimeMode !== "browser") return;
    const cards = Array.from(document.querySelectorAll(".reveal-card"));
    if (!cards.length) return;
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) entry.target.classList.add("reveal-card--visible");
      }
    }, { threshold: 0.14 });
    cards.forEach((card) => observer.observe(card));
    return () => observer.disconnect();
  }, [runtimeMode, browserView, token, browserManageableServers.length, browserInviteServers.length]);

  const userName = runtimeMode === "browser" ? browserUserName(browserUser, "Admin") : (bootstrap.currentUser.displayName || "Admin");
  const hasUnsaved = changedFields.length > 0;
  const selectedPercent = selectedSummary && selectedSummary.total > 0 ? Math.round((selectedSummary.configured / selectedSummary.total) * 100) : 0;
  const changedFieldLabels = changedFields.map((field) => field.label).slice(0, 4);
  const displayModules = useMemo(() => mergeDashboardModules(summary), [summary]);
  const activeVisualModule = useMemo(
    () => displayModules.find((item) => item.id === selectedSectionId) ?? displayModules.find((item) => item.id === selectedSection?.id) ?? null,
    [displayModules, selectedSection?.id, selectedSectionId],
  );
  const activityServerLabel = runtimeMode === "browser"
    ? (browserManageableServers.find((server) => server.id === browserSelectedGuildId)?.name ?? browserSelectedGuildId ?? "Servidor")
    : (bootstrap.context.guildId ? "Servidor atual" : "Servidor");

  if (runtimeMode === "detecting") {
    return (
      <main className="dashboard-shell dashboard-shell--home">
        <section className="auth-card">
          <div className="auth-icon">⚙️</div>
          <h2>Abrindo Dashboard</h2>
          <p>Detectando se você está dentro do Discord ou no navegador...</p>
        </section>
      </main>
    );
  }

  if (runtimeMode === "browser" && !guildId) {
    const loggedIn = Boolean(token && authState !== "needs_login");
    if (loggedIn && browserView === "servers") {
      return (
        <BrowserServerPicker
          user={browserUser}
          manageable={browserManageableServers}
          needsInvite={browserInviteServers}
          loading={loadingServers}
          message={message}
          onSelect={openBrowserServer}
          onInvite={openBrowserInvite}
          onRefresh={() => void loadBrowserServers()}
          onLogout={logoutBrowser}
        />
      );
    }
    if (loggedIn && browserView === "invite") {
      return (
        <BrowserInviteScreen
          server={browserInviteServer}
          busy={busy}
          message={message}
          onBack={() => { setBrowserView("servers"); window.history.pushState({}, "", "/dashboard"); }}
          onOpenInvite={() => void openInviteUrl()}
        />
      );
    }
    return (
      <BrowserLanding
        loggedIn={loggedIn}
        user={browserUser}
        busy={busy}
        message={message}
        onLogin={startBrowserLogin}
        onDashboard={() => { setBrowserView("servers"); window.history.pushState({}, "", "/dashboard"); void loadBrowserServers(); }}
      />
    );
  }

  return (
    <main className={`admin-dashboard admin-dashboard--${runtimeMode} admin-dashboard--${mobileView}`}>
      <div className="admin-shell">
        {authState === "ready" && (
          <aside className="admin-sidebar" aria-label="Navegação do dashboard">
            <div className="side-brand">
              <span className="side-brand-icon"><Mic size={17} /></span>
              <div>
                <strong>osaka</strong>
                <small>Dashboard</small>
              </div>
            </div>

            <button
              className={`side-link ${mobileView === "home" ? "side-link--active" : ""}`}
              onClick={() => setMobileView("home")}
            >
              <Home size={16} />
              <span>Início</span>
            </button>

            <div className="side-group">
              <span>MÓDULOS</span>
              {displayModules.filter((module) => module.group === "main").map((module) => {
                const Icon = module.icon;
                return (
                  <button
                    key={module.id}
                    className={`side-link ${selectedSectionId === module.id && mobileView === "section" ? "side-link--active" : ""}`}
                    onClick={() => openSection(module.id)}
                  >
                    <Icon size={16} />
                    <span>{module.label}</span>
                  </button>
                );
              })}
            </div>

            <div className="side-group">
              <span>SISTEMA</span>
              {displayModules.filter((module) => module.group === "system").map((module) => {
                const Icon = module.icon;
                return (
                  <button
                    key={module.id}
                    className={`side-link ${selectedSectionId === module.id && mobileView === "section" ? "side-link--active" : ""}`}
                    onClick={() => openSection(module.id)}
                  >
                    <Icon size={16} />
                    <span>{module.label}</span>
                  </button>
                );
              })}
            </div>
          </aside>
        )}

        <section className="admin-main">
          <header className="admin-topbar">
            <div className="guild-switcher">
              <span className="guild-avatar">{buildGuildAvatarText(guildId, activityServerLabel)}</span>
              <div>
                <strong>{activityServerLabel}</strong>
                <small>{runtimeMode === "browser" ? "Dashboard web" : "Activity"}</small>
              </div>
              {runtimeMode === "browser" && <ChevronDown size={15} />}
            </div>

            <div className="admin-top-actions">
              {authState === "ready" && (
                <button className="icon-button" disabled={busy} onClick={() => void refreshDashboard()} aria-label="Atualizar">
                  <RefreshCw size={17} className={busy ? "spin" : ""} />
                </button>
              )}
              {runtimeMode === "browser" && authState === "ready" && (
                <button className="icon-button icon-button--wide" onClick={() => { setBrowserSelectedGuildId(null); setBrowserView("servers"); window.history.pushState({}, "", "/dashboard"); }}>
                  <Server size={16} />
                  <span>Servidores</span>
                </button>
              )}
              <div className="user-chip">
                <span>{guildInitials(userName)}</span>
                <small>{userName}</small>
              </div>
            </div>
          </header>

          {authState !== "ready" && message && (
            <div className={`status-pill status-pill--${authState}`}>{message}</div>
          )}

          {authState === "needs_login" && (
            <section className="auth-card">
              <div className="auth-icon"><ShieldCheck size={28} /></div>
              <h2>Entrar como administrador</h2>
              <p>Autorize sua conta Discord para validar acesso ao painel deste servidor.</p>
              <button className="primary-button" disabled={busy} onClick={() => void login("consent")}>{busy ? "Autorizando..." : "Autorizar"}</button>
            </section>
          )}

          {authState === "denied" && (
            <section className="auth-card auth-card--danger">
              <div className="auth-icon"><ShieldCheck size={28} /></div>
              <h2>Sem permissão</h2>
              <p>Somente dono, administradores ou membros autorizados podem alterar configurações.</p>
              <button className="ghost-button" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Trocar conta</button>
            </section>
          )}

          {authState === "error" && (
            <section className="auth-card auth-card--danger">
              <div className="auth-icon"><HelpCircle size={28} /></div>
              <h2>Não foi possível abrir</h2>
              <p>{message}</p>
              <div className="button-row">
                <button className="ghost-button" onClick={() => void refreshDashboard()} disabled={!token || !guildId || busy}>Tentar de novo</button>
                <button className="ghost-button" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Reautenticar</button>
              </div>
            </section>
          )}

          {authState === "ready" && mobileView === "home" && (
            <section className="panel-page panel-page--home">
              <div className="page-heading">
                <span className="page-heading-icon"><LayoutGrid size={24} /></span>
                <div>
                  <h1>Painel do servidor</h1>
                  <p>Selecione um módulo abaixo para configurar.</p>
                </div>
                <button className="help-button" aria-label="Ajuda"><HelpCircle size={18} /></button>
              </div>

              <div className="admin-module-list">
                {displayModules.filter((module) => module.group === "main").map((module, index) => {
                  const Icon = module.icon;
                  return (
                    <button
                      key={module.id}
                      className={`admin-module-card admin-module-card--${statusClass(module)} ${module.available ? "" : "admin-module-card--placeholder"}`}
                      style={{ animationDelay: `${index * 32}ms` }}
                      onClick={() => openSection(module.id)}
                    >
                      <span className="module-icon-box"><Icon size={24} /></span>
                      <span className="module-text">
                        <strong>{module.label}</strong>
                        <small>{moduleDescription(module)}</small>
                      </span>
                      <ChevronRight size={21} />
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          {authState === "ready" && mobileView === "section" && (
            <section className="panel-page panel-page--section">
              <button className="mobile-back" onClick={() => setMobileView("home")}>← Início</button>

              <div className="page-heading page-heading--section">
                <span className="page-heading-icon">
                  {activeVisualModule ? <activeVisualModule.icon size={24} /> : <Settings size={24} />}
                </span>
                <div>
                  <h1>{activeVisualModule?.label ?? selectedSection?.label ?? "Configuração"}</h1>
                  <p>{activeVisualModule ? moduleDescription(activeVisualModule) : selectedSection?.description}</p>
                </div>
                <span className={`state-badge state-badge--${statusClass(selectedSummary)}`}>{sectionShortStatus(selectedSummary, Boolean(selectedSection))}</span>
              </div>

              {selectedSection ? (
                <>
                  <div className="settings-actions">
                    <button className="ghost-button" onClick={() => setDraft(values)} disabled={!hasUnsaved || saving}>Desfazer</button>
                    <button className="primary-button" onClick={() => void saveSection()} disabled={!hasUnsaved || saving}>{saving ? "Salvando..." : hasUnsaved ? `Salvar ${changedFields.length}` : "Salvo"}</button>
                  </div>

                  <div className="settings-list">
                    {selectedSection.fields.map((field) => {
                      const current = draft[field.id];
                      const changed = draft[field.id] !== values[field.id];
                      return (
                        <label key={field.id} className={`setting-row setting-row--${field.type} ${changed ? "setting-row--changed" : ""}`}>
                          <span className="setting-row-head">
                            <span>
                              <strong>{field.label}</strong>
                              {field.description && <small>{field.description}</small>}
                            </span>
                            {changed && <span className="state-badge state-badge--changed">alterado</span>}
                          </span>

                          {field.type === "boolean" ? (
                            <span className="switch-row">
                              <input
                                type="checkbox"
                                checked={Boolean(current)}
                                onChange={(event) => updateDraft(field, event.target.checked)}
                              />
                              <span>{Boolean(current) ? "Ligado" : "Desligado"}</span>
                            </span>
                          ) : field.type === "select" ? (
                            <select value={stringifyValue(current)} onChange={(event) => updateDraft(field, event.target.value)}>
                              {(field.options ?? []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                            </select>
                          ) : field.type === "textarea" ? (
                            <textarea
                              value={stringifyValue(current)}
                              maxLength={field.maxLength}
                              placeholder={field.placeholder}
                              onChange={(event) => updateDraft(field, event.target.value)}
                            />
                          ) : (
                            <input
                              type={field.type === "number" ? "number" : "text"}
                              min={field.min}
                              max={field.max}
                              maxLength={field.maxLength}
                              value={stringifyValue(current)}
                              placeholder={field.placeholder ?? (field.type === "channel" ? "ID ou menção do canal" : field.type === "role" ? "ID ou menção do cargo" : field.type === "url" ? "https://..." : "")}
                              onChange={(event) => updateDraft(field, event.target.value)}
                            />
                          )}
                          <span className="field-current">Atual: {displayValue(field, values[field.id])}</span>
                        </label>
                      );
                    })}
                  </div>
                </>
              ) : (
                <div className="module-empty">
                  <Zap size={24} />
                  <strong>Módulo pronto para integração</strong>
                  <span>Esta área ainda não possui campos liberados na API do dashboard.</span>
                </div>
              )}
            </section>
          )}

          {hasUnsaved && (
            <div className="save-dock">
              <span>{changedFields.length} alteração(ões) em {selectedSection?.label}.</span>
              <div className="button-row">
                <button className="ghost-button ghost-button--small" onClick={() => setDraft(values)} disabled={saving}>Descartar</button>
                <button className="primary-button primary-button--small" onClick={() => void saveSection()} disabled={saving}>Salvar</button>
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
