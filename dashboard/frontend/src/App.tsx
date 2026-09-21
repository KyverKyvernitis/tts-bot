import { useCallback, useMemo, useRef, useState } from "react";
import { BrowserLanding } from "./components/BrowserLanding";
import { InviteScreen } from "./components/InviteScreen";
import { LegalPage } from "./components/LegalPage";
import { ServerPicker } from "./components/ServerPicker";
import { DashboardShell } from "./components/DashboardShell";
import { FullPageLoading, LoginRequired, Notice } from "./components/AppStates";
import type { DashboardNavigationPage } from "./components/Sidebar";
import { mergeDashboardModules, type DashboardVisualModule } from "./moduleCatalog";
import {
  fetchDashboardInvite,
  patchDashboardSettings,
  clearDashboardCommandsCache,
} from "./transport/dashboardApi";
import { errorText } from "./app/errors";
import { normalizeInputValue } from "./app/dashboardValues";
import { parseRoute, routePath, type DashboardRoute, type Route } from "./app/routing";
import { changedFieldsForSection, isProtectedRoute, loginReturnPath, saveSuccessText, selectedSectionIdForRoute } from "./app/appModel";
import { useDashboardSessionBootstrap, type DashboardNotice } from "./app/useDashboardSessionBootstrap";
import { useDashboardBrowserNavigation } from "./app/useDashboardBrowserNavigation";
import { useDashboardData } from "./app/useDashboardData";
import { useDashboardRouteEffects } from "./app/useDashboardRouteEffects";
import { logoutDashboard, openDiscordLogin } from "./transport/sessionApi";
import type { DashboardFieldDefinition } from "./types/dashboard";

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  const [notice, setNotice] = useState<DashboardNotice | null>(null);
  const {
    sessionState,
    setSessionState,
    user,
    setUser,
    botIdentity,
    setBotIdentity,
    supportServer,
    bootProgress,
  } = useDashboardSessionBootstrap(setNotice);
  const {
    manageable,
    needsInvite,
    serversLoaded,
    loadingServers,
    selectedServer,
    setSelectedServer,
    sections,
    summary,
    setSummary,
    values,
    setValues,
    draft,
    setDraft,
    guildOptions,
    loadingDashboard,
    dashboardProgress,
    dashboardLoadRef,
    loadedGuildRef,
    activeGuildRef,
    loadServers,
    loadDashboard,
    resetServers,
  } = useDashboardData({ sessionState, setSessionState, setUser, setBotIdentity, setNotice });
  const [saving, setSaving] = useState(false);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [messageEditorActive, setMessageEditorActive] = useState(false);
  const [commandsRefreshToken, setCommandsRefreshToken] = useState(0);
  const savingRef = useRef(false);

  const visualModules = useMemo(() => mergeDashboardModules(summary), [summary]);
  const selectedSectionId = selectedSectionIdForRoute(route);
  const selectedSection = useMemo(() => sections.find((section) => section.id === selectedSectionId) ?? null, [sections, selectedSectionId]);
  const selectedModule = useMemo(() => route.page === "dashboard" && route.view === "module"
    ? visualModules.find((item) => item.id === selectedSectionId) ?? null
    : null, [route, selectedSectionId, visualModules]);
  const changedFields = useMemo(() => changedFieldsForSection(selectedSection, values, draft), [draft, selectedSection, values]);
  const hasUnsavedChanges = changedFields.length > 0;

  const closeMobileMenu = useCallback(() => setMobileMenuOpen(false), []);
  const openMobileMenu = useCallback(() => setMobileMenuOpen(true), []);

  const navigate = useCallback((next: Route, replace = false, bypassGuard = false) => {
    if (!bypassGuard && hasUnsavedChanges) {
      if (!window.confirm("Descartar as alterações que ainda não foram salvas?")) return false;
      setDraft(values);
    }
    const nextGuildId = next.page === "dashboard" ? next.guildId : null;
    if (activeGuildRef.current !== nextGuildId) dashboardLoadRef.current.controller?.abort();
    activeGuildRef.current = nextGuildId;
    const path = routePath(next);
    window.history[replace ? "replaceState" : "pushState"]({}, "", path);
    setRoute(next);
    setMobileMenuOpen(false);
    setNotice(null);
    return true;
  }, [hasUnsavedChanges, values]);

  useDashboardBrowserNavigation({
    route,
    selectedSectionId,
    values,
    hasUnsavedChanges,
    messageEditorActive,
    setRoute,
    setDraft,
    setNotice,
    setMobileMenuOpen,
    setMessageEditorActive,
    dashboardLoadRef,
    activeGuildRef,
  });

  useDashboardRouteEffects({
    route,
    sessionState,
    manageable,
    serversLoaded,
    sections,
    selectedSection,
    loadingDashboard,
    loadServers,
    loadDashboard,
    setSelectedServer,
    setRoute,
    setNotice,
  });

  const handleLogout = useCallback(async () => {
    if (hasUnsavedChanges && !window.confirm("Sair e descartar as alterações que ainda não foram salvas?")) return;
    try { await logoutDashboard(); } catch { /* O cookie também expira no servidor. */ }
    setUser(null);
    setSessionState("anonymous");
    resetServers();
    navigate({ page: "landing" }, true, true);
  }, [hasUnsavedChanges, navigate, resetServers]);

  const handleLogin = useCallback(() => {
    openDiscordLogin(loginReturnPath(route));
  }, [route]);

  const handleFieldChange = useCallback((field: DashboardFieldDefinition, raw: unknown) => {
    setDraft((current) => ({ ...current, [field.id]: normalizeInputValue(field, raw) }));
  }, []);

  const handleSave = useCallback(async () => {
    if (route.page !== "dashboard" || !selectedSection || changedFields.length === 0 || savingRef.current) return;
    const guildId = route.guildId;
    savingRef.current = true;
    setSaving(true);
    setNotice(null);
    try {
      const updates = Object.fromEntries(changedFields.map((field) => [field.id, draft[field.id]]));
      const result = await patchDashboardSettings(guildId, updates);
      if (activeGuildRef.current !== guildId) return;
      const mergedValues = { ...values, ...result.values };
      setValues(mergedValues);
      setDraft(mergedValues);
      if (result.summary) setSummary(result.summary);
      if (result.saved.some((id) => id === "general.bot_prefix" || id === "economy.input_mode")) clearDashboardCommandsCache(guildId);
      const count = result.saved.length;
      setNotice({
        type: "success",
        text: saveSuccessText(count, Boolean(result.summary_error)),
      });
    } catch (error) {
      if (activeGuildRef.current !== guildId) return;
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [changedFields, draft, route, selectedSection, values]);

  const openSection = useCallback((sectionId: string) => {
    if (route.page !== "dashboard") return;
    navigate({ page: "dashboard", guildId: route.guildId, view: "module", moduleId: sectionId });
  }, [navigate, route]);

  const navigateDashboardPage = useCallback((page: DashboardNavigationPage) => {
    if (route.page !== "dashboard") return;
    navigate({ page: "dashboard", guildId: route.guildId, view: page, moduleId: null });
  }, [navigate, route]);

  const openInvite = useCallback(async (guildId: string) => {
    if (inviteBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (popup) {
      popup.opener = null;
      popup.document.title = "Abrindo convite da Osaka...";
      popup.document.body.textContent = "Preparando o convite seguro do Discord...";
    }
    setInviteBusy(true);
    setNotice(null);
    try {
      const payload = await fetchDashboardInvite(guildId);
      if (!payload.invite_url) throw new Error("O backend não retornou o endereço do convite.");
      if (popup && !popup.closed) popup.location.replace(payload.invite_url);
      else window.location.assign(payload.invite_url);
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      setNotice({ type: "error", text: errorText(error) });
    } finally {
      setInviteBusy(false);
    }
  }, [inviteBusy]);

  const handleChangeServer = useCallback(() => navigate({ page: "servers" }), [navigate]);
  const handleSelectServer = useCallback((guildId: string) => {
    const server = manageable.find((item) => item.id === guildId && item.canManage && item.botPresent);
    if (!server || route.page !== "dashboard" || guildId === route.guildId) return;
    if (navigate({ page: "dashboard", guildId, view: "modules", moduleId: null })) setSelectedServer(server);
  }, [manageable, navigate, route, setSelectedServer]);
  const handleDiscard = useCallback(() => setDraft(values), [values]);
  const handleRefreshDashboard = useCallback(() => {
    if (route.page !== "dashboard") return;
    if (hasUnsavedChanges && !window.confirm("Recarregar os valores persistidos e descartar as alterações locais?")) return;
    clearDashboardCommandsCache(route.guildId);
    if (route.view === "commands") setCommandsRefreshToken((current) => current + 1);
    void loadDashboard(route.guildId, true);
  }, [hasUnsavedChanges, loadDashboard, route]);

  if (sessionState === "loading") return <FullPageLoading progress={bootProgress} />;

  if (isProtectedRoute(route) && sessionState !== "authenticated") {
    return <LoginRequired onLogin={handleLogin} onHome={() => navigate({ page: "landing" }, true, true)} />;
  }

  return <>
    {notice && <Notice type={notice.type} text={notice.text} onClose={() => setNotice(null)} />}
    {route.page === "landing" && <BrowserLanding loggedIn={sessionState === "authenticated"} user={user} bot={botIdentity} supportServer={supportServer} refreshing={loadingServers} onLogin={handleLogin} onDashboard={() => navigate({ page: "servers" })} onRefresh={() => void loadServers(true)} onLogout={() => void handleLogout()} onNavigate={(path) => navigate(parseRoute(path))} />}
    {route.page === "privacy" && <LegalPage kind="privacy" onBack={() => navigate({ page: "landing" })} />}
    {route.page === "terms" && <LegalPage kind="terms" onBack={() => navigate({ page: "landing" })} />}
    {route.page === "servers" && user && <ServerPicker manageable={manageable} needsInvite={needsInvite} loading={loadingServers} user={user} bot={botIdentity} supportServer={supportServer} onSelect={(server) => { setSelectedServer(server); navigate({ page: "dashboard", guildId: server.id, view: "general", moduleId: null }); }} onInvite={(server) => { setSelectedServer(server); navigate({ page: "invite", guildId: server.id }); }} onRefresh={() => void loadServers(true)} onLogout={() => void handleLogout()} onHome={() => navigate({ page: "landing" })} />}
    {route.page === "invite" && <InviteScreen server={selectedServer || needsInvite.find((item) => item.id === route.guildId) || null} busy={inviteBusy} onBack={() => navigate({ page: "servers" })} onOpenInvite={() => void openInvite(route.guildId)} />}
    {route.page === "dashboard" && <DashboardShell
      route={route}
      selectedServer={selectedServer}
      servers={manageable}
      user={user!}
      botIdentity={botIdentity}
      supportServer={supportServer}
      modules={visualModules}
      selectedSection={selectedSection}
      selectedModule={selectedModule}
      sectionsLoaded={loadedGuildRef.current === route.guildId && sections.length > 0}
      values={values}
      draft={draft}
      guildOptions={guildOptions}
      loading={loadingDashboard}
      loadingProgress={dashboardProgress}
      saving={saving}
      changedCount={changedFields.length}
      mobileMenuOpen={mobileMenuOpen}
      messageEditorActive={messageEditorActive}
      commandsRefreshToken={commandsRefreshToken}
      onCloseMenu={closeMobileMenu}
      onOpenMenu={openMobileMenu}
      onNavigate={navigateDashboardPage}
      onOpenModule={openSection}
      onLogout={() => void handleLogout()}
      onRefresh={handleRefreshDashboard}
      onChangeServer={handleChangeServer}
      onSelectServer={handleSelectServer}
      onFieldChange={handleFieldChange}
      onMessageEditorActiveChange={setMessageEditorActive}
      onDiscard={handleDiscard}
      onSave={() => void handleSave()}
    />}
  </>;
}
