import { useEffect, useMemo, useState } from "react";
import { HelpCircle, LoaderCircle, ShieldCheck } from "lucide-react";

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
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardSectionSummary,
  DashboardServerCard,
  DashboardUserPayload,
} from "./types/dashboard";
import { exchangeDiscordTokenRequest } from "./transport/sessionApi";
import {
  fetchDashboardBootstrap,
  fetchDashboardInvite,
  fetchDashboardOptions,
  fetchDashboardServers,
  fetchDashboardSession,
  fetchDashboardSettings,
  fetchDashboardSummary,
  patchDashboardSettings,
} from "./transport/dashboardApi";
import { mergeDashboardModules } from "./moduleCatalog";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { HomePage } from "./components/HomePage";
import { SectionEditor } from "./components/SectionEditor";
import { SaveDock } from "./components/SaveDock";
import { BrowserLanding } from "./components/BrowserLanding";
import { ServerPicker } from "./components/ServerPicker";
import { InviteScreen } from "./components/InviteScreen";

const pendingBootstrap: ActivityBootstrap = {
  sdkReady: false,
  clientId: null,
  context: { mode: "casual", instanceId: null, guildId: null, channelId: null, source: "fallback" },
  currentUser: { userId: "pending-auth", displayName: "Carregando usuário...", avatarUrl: null },
  bootDebug: [],
};

type RuntimeMode = "detecting" | "activity" | "browser";
type AuthState = "booting" | "needs_login" | "ready" | "denied" | "error";
type BrowserView = "landing" | "servers" | "invite";
type DashboardView = "home" | "section";

function isSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{15,25}$/.test(value);
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

function cleanErrorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (!raw.trim()) return "Não consegui abrir o dashboard.";
  if (raw.includes("<!doctype") || raw.includes("<html") || raw.includes("html_frontend")) {
    return "A API do dashboard ainda está respondendo com HTML do frontend. Atualize backend/proxy e tente reautenticar.";
  }
  if (raw.includes("api_proxy_returning_frontend_html")) {
    return "A autenticação caiu no frontend em vez do backend. O backend precisa responder nas rotas /token e /api/token.";
  }
  return raw.replace(/\s+/g, " ").slice(0, 260);
}

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
  if (window.location.pathname.startsWith("/dashboard/invite")) return "invite";
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

function activityUserPayload(bootstrap: ActivityBootstrap): DashboardUserPayload | null {
  const userId = bootstrap.currentUser.userId;
  if (!userId || userId === "pending-auth") return null;
  return {
    id: userId,
    username: bootstrap.currentUser.displayName,
    global_name: bootstrap.currentUser.displayName,
    avatar: null,
    avatarUrl: bootstrap.currentUser.avatarUrl ?? null,
  };
}

function guildLabelFromServers(guildId: string | null, servers: DashboardServerCard[], fallback: string) {
  if (!guildId) return fallback;
  return servers.find((server) => server.id === guildId)?.name ?? fallback;
}

function guildIconFromServers(guildId: string | null, servers: DashboardServerCard[]): string | null {
  if (!guildId) return null;
  return servers.find((server) => server.id === guildId)?.icon ?? null;
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<ActivityBootstrap>(pendingBootstrap);
  const [token, setToken] = useState<string | null>(() => readCachedToken());
  const [authState, setAuthState] = useState<AuthState>("booting");
  const [message, setMessage] = useState<string>("Abrindo Dashboard...");
  const [sections, setSections] = useState<DashboardSectionDefinition[]>([]);
  const [summary, setSummary] = useState<DashboardSectionSummary[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [selectedSectionId, setSelectedSectionId] = useState<string>("general");
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dashboardView, setDashboardView] = useState<DashboardView>("home");
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>("detecting");
  const [browserView, setBrowserView] = useState<BrowserView>(() => initialBrowserView());
  const [browserUser, setBrowserUser] = useState<DashboardUserPayload | null>(null);
  const [browserManageableServers, setBrowserManageableServers] = useState<DashboardServerCard[]>([]);
  const [browserInviteServers, setBrowserInviteServers] = useState<DashboardServerCard[]>([]);
  const [browserSelectedGuildId, setBrowserSelectedGuildId] = useState<string | null>(() => readBrowserGuildFromLocation());
  const [activityGuildOverride, setActivityGuildOverride] = useState<string | null>(null);
  const [browserInviteServer, setBrowserInviteServer] = useState<DashboardServerCard | null>(null);
  const [loadingServers, setLoadingServers] = useState(false);
  const [showServerPicker, setShowServerPicker] = useState(false);
  const [guildOptions, setGuildOptions] = useState<DashboardOptionsPayload | null>(null);

  const activityGuildId = bootstrap.context.guildId;
  const guildId = runtimeMode === "browser"
    ? browserSelectedGuildId
    : (activityGuildOverride ?? activityGuildId);

  const selectedSection = useMemo(
    () => sections.find((section) => section.id === selectedSectionId) ?? null,
    [sections, selectedSectionId],
  );
  const selectedSummary = useMemo(
    () => summary.find((item) => item.id === selectedSection?.id),
    [summary, selectedSection?.id],
  );
  const displayModules = useMemo(() => mergeDashboardModules(summary), [summary]);
  const selectedModule = useMemo(
    () => displayModules.find((item) => item.id === selectedSectionId) ?? displayModules.find((item) => item.id === selectedSection?.id) ?? null,
    [displayModules, selectedSection?.id, selectedSectionId],
  );
  const changedFields = useMemo(() => {
    if (!selectedSection) return [];
    return selectedSection.fields.filter((field) => draft[field.id] !== values[field.id]);
  }, [draft, selectedSection, values]);

  const userName = runtimeMode === "browser"
    ? browserUserName(browserUser, "Admin")
    : (bootstrap.currentUser.displayName || "Admin");
  const userAvatarUrl = runtimeMode === "browser"
    ? (browserUser?.avatarUrl ?? null)
    : (bootstrap.currentUser.avatarUrl ?? null);
  const serverLabel = runtimeMode === "browser"
    ? guildLabelFromServers(guildId, browserManageableServers, guildId ?? "Servidor")
    : guildLabelFromServers(guildId, browserManageableServers, activityGuildOverride ? (guildId ?? "Servidor") : "Servidor atual");
  const serverIconUrl = guildIconFromServers(guildId, browserManageableServers);

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
      setBrowserUser(payload.user ?? browserUser ?? activityUserPayload(bootstrap));
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
        setShowServerPicker(false);
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

  function resetDashboardData() {
    setDashboardView("home");
    setSections([]);
    setSummary([]);
    setValues({});
    setDraft({});
    setGuildOptions(null);
  }

  function openServer(server: DashboardServerCard) {
    resetDashboardData();
    setMessage(`Abrindo ${server.name}...`);
    if (runtimeMode === "activity") {
      setActivityGuildOverride(server.id);
      setShowServerPicker(false);
      return;
    }
    setBrowserSelectedGuildId(server.id);
    setBrowserView("servers");
    window.history.pushState({}, "", `/dashboard/${server.id}`);
  }

  function openServerPicker() {
    if (runtimeMode === "browser") {
      setBrowserSelectedGuildId(null);
      setBrowserView("servers");
      window.history.pushState({}, "", "/dashboard");
    } else {
      setShowServerPicker(true);
    }
    void loadBrowserServers();
  }

  function openBrowserInvite(server: DashboardServerCard) {
    setBrowserInviteServer(server);
    setBrowserView("invite");
    setMessage("Convide o bot para liberar a configuração deste servidor.");
    if (runtimeMode === "browser") window.history.pushState({}, "", "/dashboard/invite");
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
    setActivityGuildOverride(null);
    setBrowserInviteServer(null);
    setBrowserView("landing");
    setShowServerPicker(false);
    setAuthState("needs_login");
    setMessage("Sessão encerrada.");
    if (runtimeMode === "browser") window.history.pushState({}, "", "/");
  }

  async function loadGuildOptions(accessToken: string, targetGuildId: string) {
    try {
      const payload = await fetchDashboardOptions(accessToken, targetGuildId);
      setGuildOptions(payload);
    } catch (error) {
      setGuildOptions({ ok: false, channels: [], roles: [], error: cleanErrorText(error) });
    }
  }

  async function loadDashboard(accessToken: string, targetGuildId: string) {
    setBusy(true);
    setMessage("Carregando configurações do servidor...");
    void loadGuildOptions(accessToken, targetGuildId);
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
    const found = sections.some((section) => section.id === sectionId);
    if (!found) {
      setMessage("Este módulo ainda não foi liberado pela API do dashboard.");
      return;
    }
    setSelectedSectionId(sectionId);
    setDashboardView("section");
  }

  useEffect(() => {
    let cancelled = false;
    bootstrapDiscord().then(async (result) => {
      if (cancelled) return;
      setBootstrap(result);

      if (!result.sdkReady) {
        setRuntimeMode("browser");
        const handledOAuth = await finishBrowserOAuthIfNeeded();
        if (cancelled || handledOAuth) return;

        const cachedToken = readCachedToken();
        if (cachedToken) {
          setToken(cachedToken);
          await hydrateBrowserSession(cachedToken);
          const guildFromUrl = readBrowserGuildFromLocation();
          if (guildFromUrl) setBrowserSelectedGuildId(guildFromUrl);
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
    if (!token) return;
    if (runtimeMode === "browser" && browserView === "servers" && !browserSelectedGuildId) {
      void loadBrowserServers();
    }
    if (runtimeMode === "activity" && showServerPicker) {
      void loadBrowserServers();
    }
  }, [runtimeMode, token, browserView, browserSelectedGuildId, showServerPicker]);

  if (runtimeMode === "detecting") {
    return (
      <div className="osk-root">
        <main className="osk-app">
          <div className="osk-main">
            <section className="osk-auth">
              <div className="osk-auth-icon"><LoaderCircle size={28} className="osk-spin" /></div>
              <h2>Abrindo Dashboard</h2>
              <p>Detectando se você está dentro do Discord ou no navegador...</p>
            </section>
          </div>
        </main>
      </div>
    );
  }

  const loggedIn = Boolean(token && authState !== "needs_login");

  if (runtimeMode === "browser" && !guildId) {
    if (loggedIn && browserView === "servers") {
      return (
        <div className="osk-root">
          <ServerPicker
            user={browserUser}
            manageable={browserManageableServers}
            needsInvite={browserInviteServers}
            loading={loadingServers}
            onSelect={openServer}
            onInvite={openBrowserInvite}
            onRefresh={() => void loadBrowserServers()}
            onLogout={logoutBrowser}
          />
        </div>
      );
    }
    if (loggedIn && browserView === "invite") {
      return (
        <div className="osk-root">
          <InviteScreen
            server={browserInviteServer}
            busy={busy}
            onBack={() => { setBrowserView("servers"); window.history.pushState({}, "", "/dashboard"); }}
            onOpenInvite={() => void openInviteUrl()}
          />
        </div>
      );
    }
    return (
      <div className="osk-root">
        <BrowserLanding
          loggedIn={loggedIn}
          user={browserUser}
          onLogin={startBrowserLogin}
          onDashboard={() => { setBrowserView("servers"); window.history.pushState({}, "", "/dashboard"); void loadBrowserServers(); }}
        />
      </div>
    );
  }

  if (runtimeMode === "activity" && showServerPicker && authState === "ready") {
    return (
      <div className="osk-root">
        <ServerPicker
          user={browserUser ?? activityUserPayload(bootstrap)}
          manageable={browserManageableServers}
          needsInvite={browserInviteServers}
          loading={loadingServers}
          onSelect={openServer}
          onInvite={openBrowserInvite}
          onRefresh={() => void loadBrowserServers()}
          onLogout={logoutBrowser}
        />
      </div>
    );
  }

  return (
    <div className="osk-root">
      <main className="osk-app">
        {authState === "ready" && (
          <Sidebar
            modules={displayModules}
            selectedSectionId={selectedSectionId}
            view={dashboardView}
            onHome={() => setDashboardView("home")}
            onSelect={openSection}
          />
        )}

        <div className="osk-main">
          {authState === "ready" && (
            <Topbar
              guildName={serverLabel}
              guildIcon={serverIconUrl}
              runtime="browser"
              userName={userName}
              userAvatar={userAvatarUrl}
              busy={busy}
              onRefresh={() => void refreshDashboard()}
              onChangeServer={token ? openServerPicker : undefined}
            />
          )}

          {authState !== "ready" && message && (
            <div className="osk-status" data-tone={authState === "error" || authState === "denied" ? "error" : "info"}>{message}</div>
          )}

          {authState === "needs_login" && (
            <section className="osk-auth">
              <div className="osk-auth-icon"><ShieldCheck size={28} /></div>
              <h2>Entrar como administrador</h2>
              <p>Autorize sua conta Discord para validar acesso ao painel deste servidor.</p>
              <button className="osk-btn osk-btn--primary" disabled={busy} onClick={() => void login("consent")}>{busy ? "Autorizando..." : "Autorizar"}</button>
            </section>
          )}

          {authState === "denied" && (
            <section className="osk-auth">
              <div className="osk-auth-icon"><ShieldCheck size={28} /></div>
              <h2>Sem permissão</h2>
              <p>Somente dono, administradores ou membros autorizados podem alterar configurações.</p>
              <button className="osk-btn" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Trocar conta</button>
            </section>
          )}

          {authState === "error" && (
            <section className="osk-auth">
              <div className="osk-auth-icon"><HelpCircle size={28} /></div>
              <h2>Não foi possível abrir</h2>
              <p>{message}</p>
              <div className="osk-invite-actions">
                <button className="osk-btn" onClick={() => void refreshDashboard()} disabled={!token || !guildId || busy}>Tentar de novo</button>
                <button className="osk-btn" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Reautenticar</button>
              </div>
            </section>
          )}

          {authState === "ready" && message && (
            <div className="osk-status">{message}</div>
          )}

          {authState === "ready" && dashboardView === "home" && (
            <HomePage guildName={serverLabel} modules={displayModules} onOpen={openSection} />
          )}

          {authState === "ready" && dashboardView === "section" && selectedSection && (
            <SectionEditor
              section={selectedSection}
              module={selectedModule}
              summary={selectedSummary}
              values={values}
              draft={draft}
              guildOptions={guildOptions}
              onChange={updateDraft}
              onBack={() => setDashboardView("home")}
            />
          )}

          {authState === "ready" && selectedSection && (
            <SaveDock
              changedCount={changedFields.length}
              sectionLabel={selectedSection.label}
              saving={saving}
              onDiscard={() => setDraft(values)}
              onSave={() => void saveSection()}
            />
          )}
        </div>
      </main>
    </div>
  );
}
