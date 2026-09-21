import { FieldFeedbackContext } from "./module-settings/FieldFeedback";
import { CommandsPage } from "./CommandsPage";
import { GeneralPage } from "./GeneralPage";
import { ModulesPage } from "./HomePage";
import { SaveDock } from "./SaveDock";
import { SectionEditor } from "./SectionEditor";
import { Sidebar, type DashboardNavigationPage } from "./Sidebar";
import { Topbar } from "./Topbar";
import { DashboardMobileNav } from "./DashboardMobileNav";
import { LoadingProgress, LoadingVisual } from "./VisualTemplates";
import type { DashboardVisualModule } from "../moduleCatalog";
import type {
  DashboardFieldDefinition,
  DashboardOptionsPayload,
  DashboardSectionDefinition,
  DashboardServerCard,
  DashboardSupportServerPayload,
  DashboardUserPayload,
} from "../types/dashboard";
import type { DashboardRoute } from "../app/routing";

export interface DashboardShellProps {
  route: DashboardRoute;
  selectedServer: DashboardServerCard | null;
  servers: DashboardServerCard[];
  user: DashboardUserPayload;
  botIdentity: DashboardUserPayload | null;
  supportServer: DashboardSupportServerPayload | null;
  modules: DashboardVisualModule[];
  selectedSection: DashboardSectionDefinition | null;
  selectedModule: DashboardVisualModule | null;
  sectionsLoaded: boolean;
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  guildOptions: DashboardOptionsPayload | null;
  fieldErrors?: Record<string, string>;
  optionsBusy?: boolean;
  onRetryOptions?(): void;
  loading: boolean;
  loadingProgress: number;
  saving: boolean;
  changedCount: number;
  mobileMenuOpen: boolean;
  messageEditorActive: boolean;
  commandsRefreshToken: number;
  onCloseMenu(): void;
  onOpenMenu(): void;
  onNavigate(page: DashboardNavigationPage): void;
  onOpenModule(sectionId: string): void;
  onLogout(): void;
  onRefresh(): void;
  onChangeServer(): void;
  onSelectServer(guildId: string): void;
  onFieldChange(field: DashboardFieldDefinition, raw: unknown): void;
  onMessageEditorActiveChange(active: boolean): void;
  onDiscard(): void;
  onSave(): void;
}

export function DashboardShell({
  route,
  selectedServer,
  servers,
  user,
  botIdentity,
  supportServer,
  modules,
  selectedSection,
  selectedModule,
  sectionsLoaded,
  values,
  draft,
  guildOptions,
  fieldErrors = {},
  optionsBusy,
  onRetryOptions,
  loading,
  loadingProgress,
  saving,
  changedCount,
  mobileMenuOpen,
  messageEditorActive,
  commandsRefreshToken,
  onCloseMenu,
  onOpenMenu,
  onNavigate,
  onOpenModule,
  onLogout,
  onRefresh,
  onChangeServer,
  onSelectServer,
  onFieldChange,
  onMessageEditorActiveChange,
  onDiscard,
  onSave,
}: DashboardShellProps) {
  const guildName = selectedServer?.name || `Servidor ${route.guildId.slice(-6)}`;
  const guildIcon = selectedServer?.icon || null;
  const botName = botIdentity?.global_name || botIdentity?.username || "Osaka";
  const activePage: DashboardNavigationPage = route.view === "module" ? "modules" : route.view;
  const editableSection = route.view === "general" || route.view === "module" ? selectedSection : null;

  return <FieldFeedbackContext.Provider value={{ errors: fieldErrors, optionsBusy, onRetryOptions }}><div className="osk-dashboard-shell" data-has-draft={changedCount > 0 || undefined}>
    <Sidebar
      activePage={activePage}
      mobileOpen={mobileMenuOpen}
      botName={botName}
      botAvatarUrl={botIdentity?.avatarUrl}
      onCloseMobile={onCloseMenu}
      onOpenMobile={onOpenMenu}
      gestureDisabled={messageEditorActive}
      onNavigate={onNavigate}
      onLogout={onLogout}
    />
    <div className="osk-dashboard-main">
      <Topbar guildId={route.guildId} guildName={guildName} guildIcon={guildIcon} servers={servers} botName={botName} botAvatarUrl={botIdentity?.avatarUrl} user={user} supportServer={supportServer} busy={loading} onRefresh={onRefresh} onChangeServer={onChangeServer} onSelectServer={onSelectServer} onLogout={onLogout} onOpenMenu={onOpenMenu} />
      <main className="osk-dashboard-content">
        <div key={`${route.view}:${route.moduleId || "root"}`} className="osk-page-motion">
          {loading && !sectionsLoaded ? <DashboardLoading progress={loadingProgress} /> : route.view === "general" && selectedSection ? (
            <GeneralPage
              section={selectedSection}
              values={values}
              draft={draft}
              guildOptions={guildOptions}
              guildName={guildName}
              guildIcon={guildIcon}
              onChange={onFieldChange}
            />
          ) : route.view === "commands" ? (
            <CommandsPage guildId={route.guildId} refreshToken={commandsRefreshToken} />
          ) : route.view === "module" && selectedSection ? (
            <SectionEditor
              section={selectedSection}
              module={selectedModule}
              values={values}
              draft={draft}
              guildOptions={guildOptions}
              previewBotName={botName}
              previewBotAvatarUrl={botIdentity?.avatarUrl}
              previewGuildName={guildName}
              previewGuildAvatarUrl={guildIcon}
              onChange={onFieldChange}
              onMessageEditorActiveChange={onMessageEditorActiveChange}
              onBack={() => onNavigate("modules")}
            />
          ) : <ModulesPage modules={modules} onOpen={onOpenModule} />}
        </div>
      </main>
    </div>
    {!messageEditorActive && editableSection && <SaveDock changedCount={changedCount} sectionLabel={editableSection.label} saving={saving} onDiscard={onDiscard} onSave={onSave} />}
    {!messageEditorActive && <DashboardMobileNav activePage={activePage} onNavigate={onNavigate} />}
  </div></FieldFeedbackContext.Provider>;
}

function DashboardLoading({ progress }: { progress: number }) {
  return <div className="osk-dashboard-loading" aria-busy="true"><LoadingVisual size={28} /><LoadingProgress progress={progress} label="Carregando configurações do servidor" /></div>;
}
