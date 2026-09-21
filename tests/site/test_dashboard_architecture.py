from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboard"
FRONTEND = DASHBOARD / "frontend"
BACKEND = DASHBOARD / "backend"


def _package(path: Path) -> dict[str, object]:
    return json.loads((path / "package.json").read_text(encoding="utf-8"))


def _relative_imports(source: str) -> list[str]:
    patterns = [
        r"\bfrom\s+[\"'](\.[^\"']+)[\"']",
        r"\bimport\s+[\"'](\.[^\"']+)[\"']",
        r"\bimport\s*\(\s*[\"'](\.[^\"']+)[\"']\s*\)",
    ]
    imports: list[str] = []
    for pattern in patterns:
        imports.extend(re.findall(pattern, source))
    return imports


def _resolves_local_import(source_file: Path, specifier: str) -> bool:
    base = source_file.parent / specifier
    candidates = [base]
    if base.suffix == ".js":
        candidates.extend([base.with_suffix(".ts"), base.with_suffix(".tsx")])
    elif not base.suffix:
        candidates.extend(base.with_suffix(ext) for ext in (".ts", ".tsx", ".js", ".mjs", ".css", ".json"))
        candidates.extend(base / f"index{ext}" for ext in (".ts", ".tsx", ".js", ".mjs"))
    return any(candidate.is_file() for candidate in candidates)


def test_dashboard_has_canonical_frontend_and_backend_packages() -> None:
    front = _package(FRONTEND)
    back = _package(BACKEND)
    assert front["name"] == "osaka-dashboard-web"
    assert back["name"] == "osaka-dashboard-server"
    assert (FRONTEND / "src/App.tsx").is_file()
    assert (BACKEND / "src/index.ts").is_file()


def test_legacy_bridges_target_only_canonical_dashboard_projects() -> None:
    front_path = ROOT / "activity/sinuca"
    back_path = ROOT / "activity/sinuca-server"
    present = (front_path.exists(), back_path.exists())
    assert present in {(True, True), (False, False)}, "layout parcial de bridges não é válido"
    if not any(present):
        return

    front_bridge = (front_path / "scripts/dashboard-bridge-build.mjs").read_text(encoding="utf-8")
    back_bridge = (back_path / "scripts/dashboard-bridge-build.mjs").read_text(encoding="utf-8")
    assert 'resolve(repoDir, "dashboard/frontend")' in front_bridge
    assert 'resolve(repoDir, "dashboard/backend")' in back_bridge
    assert 'skipFiles = new Set(["package.json", "scripts/dashboard-bridge-build.mjs"])' in front_bridge
    assert 'skipFiles = new Set(["package.json", "scripts/dashboard-bridge-build.mjs"])' in back_bridge


def test_bridge_dependency_contracts_match_canonical_projects() -> None:
    pairs = [
        (ROOT / "activity/sinuca", FRONTEND),
        (ROOT / "activity/sinuca-server", BACKEND),
    ]
    present = tuple(path.exists() for path, _ in pairs)
    assert present in {(True, True), (False, False)}, "layout parcial de bridges não é válido"
    if not any(present):
        return

    for bridge_path, canonical_path in pairs:
        bridge = _package(bridge_path)
        canonical = _package(canonical_path)
        for key in ("name", "version", "type", "engines", "dependencies", "devDependencies"):
            assert bridge.get(key) == canonical.get(key), f"{bridge_path}: contrato {key} divergiu"
        assert bridge.get("scripts", {}).get("build") == "node scripts/dashboard-bridge-build.mjs"


def test_all_dashboard_relative_imports_resolve_inside_source_tree() -> None:
    unresolved: list[str] = []
    for source_file in sorted(DASHBOARD.rglob("*")):
        if source_file.suffix not in {".ts", ".tsx", ".js", ".mjs"} or not source_file.is_file():
            continue
        source = source_file.read_text(encoding="utf-8")
        for specifier in _relative_imports(source):
            if not _resolves_local_import(source_file, specifier):
                unresolved.append(f"{source_file.relative_to(ROOT)} -> {specifier}")
    assert unresolved == []


def test_canonical_dashboard_source_does_not_reference_legacy_activity_paths() -> None:
    offenders: list[str] = []
    for source_file in DASHBOARD.rglob("*"):
        if source_file.suffix not in {".ts", ".tsx", ".js", ".mjs", ".json", ".css", ".html"} or not source_file.is_file():
            continue
        source = source_file.read_text(encoding="utf-8", errors="replace")
        if "activity/sinuca" in source or "activity/sinuca-server" in source:
            offenders.append(str(source_file.relative_to(ROOT)))
    assert offenders == []


def test_modularization_boundaries_do_not_regress_into_monoliths() -> None:
    limits = {
        FRONTEND / "src/App.tsx": 280,
        FRONTEND / "src/components/message-editor/MessageEditor.tsx": 400,
        FRONTEND / "src/components/SectionEditor.tsx": 190,
        FRONTEND / "src/components/Sidebar.tsx": 100,
        FRONTEND / "src/components/SmartSelect.tsx": 80,
        FRONTEND / "src/components/AccountMenu.tsx": 140,
        BACKEND / "src/routes/registerDashboardRoutes.ts": 80,
        BACKEND / "src/routes/dashboardApiRoutes.ts": 30,
        BACKEND / "src/routes/dashboardAuthRoutes.ts": 40,
        BACKEND / "src/services/dashboardConfigService.ts": 70,
        BACKEND / "src/services/discordAuthService.ts": 60,
        BACKEND / "src/services/discordAccessService.ts": 20,
        BACKEND / "src/services/dashboardSessionService.ts": 100,
        BACKEND / "src/services/dashboardCommandsService.ts": 40,
    }
    for path, maximum in limits.items():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= maximum, f"{path.relative_to(ROOT)} voltou a concentrar responsabilidades: {lines}>{maximum}"


def test_new_message_editor_modules_are_present_and_used() -> None:
    editor_dir = FRONTEND / "src/components/message-editor"
    editor_source = (editor_dir / "MessageEditor.tsx").read_text(encoding="utf-8")
    hook_source = (editor_dir / "useMessageEditorHistory.ts").read_text(encoding="utf-8")
    for module in (
        "useMessageEditorContextPlacement",
        "useMessageEditorDialogLifecycle",
        "useMessageEditorHistory",
        "useMessageEditorJsonState",
        "useMessageEditorTextEditing",
        "MessageEditorSurface",
        "messageEditorFields",
        "useMessageEditorNavigation",
        "useMessageEditorResetEffect",
        "useMessageEditorViewState",
    ):
        assert module in editor_source
        assert any(editor_dir.glob(f"{module}.ts*"))
    derived_source = (editor_dir / "messageEditorDerivedState.ts").read_text(encoding="utf-8")
    assert "messageEditorDerivedState" in editor_source
    assert "messageEditorInteractionModel" in derived_source
    assert (editor_dir / "messageEditorDerivedState.ts").is_file()
    surface_source = (editor_dir / "MessageEditorSurface.tsx").read_text(encoding="utf-8")
    for module in ("MessageEditorHeader", "MessageEditorCanvas", "MessageEditorTextDock", "MessageEditorContextPanel"):
        assert module in surface_source
        assert any(editor_dir.glob(f"{module}.ts*"))
    context_source = (editor_dir / "useMessageEditorContextPlacement.ts").read_text(encoding="utf-8")
    assert "messageEditorLayout" in context_source
    assert (editor_dir / "messageEditorLayout.ts").is_file()
    assert "messageEditorHistory" in hook_source
    assert (editor_dir / "messageEditorHistory.ts").is_file()



def test_extracted_ui_modules_remain_bounded() -> None:
    limits = {
        FRONTEND / "src/components/SectionEditorPanels.tsx": 180,
        FRONTEND / "src/components/useSectionMessageEditor.ts": 90,
        FRONTEND / "src/components/sectionGroupPresentation.tsx": 100,
        FRONTEND / "src/components/message-editor/useMessageEditorDialogLifecycle.ts": 70,
        FRONTEND / "src/components/message-editor/useMessageEditorDialogClose.ts": 140,
        FRONTEND / "src/components/message-editor/useMessageEditorDialogEnvironment.ts": 180,
        FRONTEND / "src/components/message-editor/messageEditorDialogModel.ts": 50,
        FRONTEND / "src/components/message-editor/useMessageEditorContextPlacement.ts": 110,
        FRONTEND / "src/components/message-editor/useMessageEditorHistory.ts": 110,
        FRONTEND / "src/components/message-editor/useMessageEditorJsonState.ts": 90,
        FRONTEND / "src/components/message-editor/useMessageEditorJsonWorkflow.ts": 130,
        FRONTEND / "src/components/message-editor/useMessageEditorWorkspaceEffects.ts": 80,
        FRONTEND / "src/components/message-editor/useMessageEditorTextEditing.ts": 190,
        FRONTEND / "src/components/message-editor/useMessageEditorNavigation.ts": 170,
        FRONTEND / "src/components/message-editor/useMessageEditorViewState.ts": 50,
        FRONTEND / "src/components/message-editor/useMessageEditorResetEffect.ts": 50,
        FRONTEND / "src/components/message-editor/MessageEditorHeader.tsx": 80,
        FRONTEND / "src/components/message-editor/MessageEditorCanvas.tsx": 40,
        FRONTEND / "src/components/message-editor/MessageEditorTextDock.tsx": 60,
        FRONTEND / "src/components/message-editor/MessageEditorContextPanel.tsx": 180,
        FRONTEND / "src/components/message-editor/MessageEditorSurface.tsx": 100,
        FRONTEND / "src/components/message-editor/MessageVisualEditor.tsx": 100,
        FRONTEND / "src/components/message-editor/MessageContextualSelect.tsx": 220,
        FRONTEND / "src/components/message-editor/messageVisualEditorModel.ts": 40,
        FRONTEND / "src/components/message-editor/messageEditorFields.ts": 60,
        FRONTEND / "src/components/message-editor/messageEditorDerivedState.ts": 70,
        FRONTEND / "src/app/useDashboardBrowserNavigation.ts": 120,
        FRONTEND / "src/app/useDashboardData.ts": 180,
        FRONTEND / "src/app/useDashboardRouteEffects.ts": 90,
        FRONTEND / "src/components/sidebar/useSidebarDrawer.ts": 80,
        FRONTEND / "src/components/sidebar/useSidebarAccessibility.ts": 100,
        FRONTEND / "src/components/sidebar/useSidebarGestureEvents.ts": 190,
        FRONTEND / "src/components/sidebar/sidebarGestureModel.ts": 80,
        FRONTEND / "src/components/smart-select/useSmartSelect.ts": 200,
        FRONTEND / "src/components/smart-select/smartSelectModel.ts": 90,
        FRONTEND / "src/components/account-menu/useAccountMenu.ts": 170,
        FRONTEND / "src/components/account-menu/accountMenuModel.ts": 50,
        BACKEND / "src/routes/dashboardAuthGuard.ts": 90,
        BACKEND / "src/routes/dashboardHealthRoute.ts": 40,
        BACKEND / "src/routes/dashboardOAuthRoutes.ts": 110,
        BACKEND / "src/routes/dashboardOAuthModel.ts": 30,
        BACKEND / "src/routes/dashboardPublicIdentityRoute.ts": 40,
        BACKEND / "src/routes/dashboardSessionRoutes.ts": 60,
        BACKEND / "src/services/discordGuildAccess.ts": 120,
        BACKEND / "src/services/discordGuildOptionsService.ts": 60,
        BACKEND / "src/services/discordServerDiscovery.ts": 80,
        BACKEND / "src/services/dashboardSessionPrimitives.ts": 170,
        BACKEND / "src/services/dashboardSessionModel.ts": 60,
        BACKEND / "src/services/dashboardFullLoader.ts": 80,
        BACKEND / "src/services/discordAuthConfig.ts": 60,
        BACKEND / "src/services/discordCache.ts": 70,
        BACKEND / "src/services/discordIdentityService.ts": 110,
        BACKEND / "src/services/discordAccessService.ts": 220,
        BACKEND / "src/services/singleFlight.ts": 30,
        BACKEND / "src/services/dashboardCommandsCatalog.ts": 50,
        BACKEND / "src/services/dashboardCommandsModel.ts": 100,
        BACKEND / "src/services/dashboardWorkerAvailability.ts": 140,
        BACKEND / "src/services/dashboardCommandsTypes.ts": 80,
        BACKEND / "src/config/dashboardColorRoleDefaults.ts": 60,
        BACKEND / "src/config/dashboardFormsDefaults.ts": 60,
        BACKEND / "src/config/dashboardTicketsDefaults.ts": 90,
        BACKEND / "src/config/dashboardGuildDefaults.ts": 70,
        BACKEND / "src/config/dashboardFeatureDefaults.ts": 90,
        BACKEND / "src/config/dashboardColorValueCodec.ts": 120,
        BACKEND / "src/config/dashboardFormValueCodec.ts": 50,
        BACKEND / "src/services/dashboardConfigModel.ts": 150,
        BACKEND / "src/services/dashboardConfigRepository.ts": 110,
        FRONTEND / "src/components/message-editor/MessagePreviewVariants.tsx": 20,
        FRONTEND / "src/components/message-editor/MessagePreviewPrimitives.tsx": 20,
        FRONTEND / "src/components/message-editor/MessagePreviewEmbedVariants.tsx": 100,
        FRONTEND / "src/components/message-editor/MessagePreviewV2Variants.tsx": 200,
        FRONTEND / "src/components/message-editor/MessagePreviewGenericVariant.tsx": 50,
        FRONTEND / "src/components/message-editor/ColorRolesPanelPreview.tsx": 130,
        FRONTEND / "src/components/message-editor/MessagePreviewEditablePrimitives.tsx": 190,
        FRONTEND / "src/components/message-editor/MessagePreviewMediaPrimitives.tsx": 140,
        FRONTEND / "src/components/message-editor/MessagePreviewAccentControl.tsx": 40,
        FRONTEND / "src/components/message-editor/messageEditorUtils.ts": 20,
        FRONTEND / "src/components/message-editor/messageEditorJson.ts": 190,
        FRONTEND / "src/components/message-editor/messageEditorMedia.ts": 110,
        FRONTEND / "src/components/message-editor/messageEditorTemplate.ts": 30,
    }
    for path, maximum in limits.items():
        assert path.is_file(), f"módulo extraído ausente: {path.relative_to(ROOT)}"
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= maximum, f"{path.relative_to(ROOT)} cresceu além do limite arquitetural: {lines}>{maximum}"


def test_sidebar_keeps_drawer_mechanics_outside_presentational_component() -> None:
    sidebar = (FRONTEND / "src/components/Sidebar.tsx").read_text(encoding="utf-8")
    drawer = (FRONTEND / "src/components/sidebar/useSidebarDrawer.ts").read_text(encoding="utf-8")
    accessibility = (FRONTEND / "src/components/sidebar/useSidebarAccessibility.ts").read_text(encoding="utf-8")
    gestures = (FRONTEND / "src/components/sidebar/useSidebarGestureEvents.ts").read_text(encoding="utf-8")
    model = (FRONTEND / "src/components/sidebar/sidebarGestureModel.ts").read_text(encoding="utf-8")
    assert "useSidebarDrawer" in sidebar
    assert "useSidebarAccessibility" in drawer
    assert "useSidebarGestureEvents" in drawer
    assert "addEventListener(\"pointerdown\"" not in sidebar
    assert "addEventListener(\"pointerdown\"" not in drawer
    assert "addEventListener(\"pointerdown\"" in gestures
    assert "addEventListener(\"keydown\"" in accessibility
    assert "document.body.style.overflow" in accessibility
    assert "aria-hidden" in accessibility
    assert "sidebarGestureAxis" in gestures
    assert "sidebarShouldOpen" in gestures
    assert "sidebarShouldClose" in gestures
    assert "SIDEBAR_AXIS_RATIO" in model

def test_app_bootstrap_and_decisions_are_extracted_from_root_component() -> None:
    app = (FRONTEND / "src/App.tsx").read_text(encoding="utf-8")
    expected = {
        FRONTEND / "src/app/appModel.ts": 60,
        FRONTEND / "src/app/sessionModel.ts": 40,
        FRONTEND / "src/app/useDashboardSessionBootstrap.ts": 110,
        FRONTEND / "src/app/useDashboardSave.ts": 100,
        FRONTEND / "src/app/dashboardFormValidation.ts": 80,
    }
    for path, maximum in expected.items():
        assert path.is_file(), f"módulo de App ausente: {path.relative_to(ROOT)}"
        assert len(path.read_text(encoding="utf-8").splitlines()) <= maximum
    assert "useDashboardSessionBootstrap" in app
    assert "selectedSectionIdForRoute" in app
    assert "useDashboardSave" in app
    assert "saveSuccessText" in (FRONTEND / "src/app/useDashboardSave.ts").read_text(encoding="utf-8")
    assert "useDashboardData" in app
    assert "fetchDashboardFull" not in app
    assert "fetchDashboardServers" not in app


def test_backend_catalog_is_composed_from_bounded_section_modules() -> None:
    catalog = BACKEND / "src/config/dashboardCatalog.ts"
    shared = BACKEND / "src/config/dashboardCatalogShared.ts"
    section_dir = BACKEND / "src/config/sections"
    source = catalog.read_text(encoding="utf-8")
    expected = [
        "dashboardGeneralSection",
        "dashboardWelcomeSection",
        "dashboardFormsSection",
        "dashboardTicketsSection",
        "dashboardColorRolesSection",
        "dashboardBirthdaySection",
        "dashboardTtsSection",
    ]
    assert len(source.splitlines()) <= 40
    assert shared.is_file() and len(shared.read_text(encoding="utf-8").splitlines()) <= 220
    for module in expected:
        path = section_dir / f"{module}.ts"
        assert path.is_file(), f"seção do catálogo ausente: {path.relative_to(ROOT)}"
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 130
        assert module in source


def test_dashboard_tree_contains_no_generated_dependency_or_build_artifacts() -> None:
    forbidden_names = {"node_modules", "dist", ".vite", ".cache", "coverage"}
    offenders = [
        str(path.relative_to(ROOT))
        for path in DASHBOARD.rglob("*")
        if path.name in forbidden_names or path.name.endswith(".tsbuildinfo")
    ]
    assert offenders == []


def _frontend_transport_paths(source: str) -> set[str]:
    paths: set[str] = set()
    fetch_pattern = re.compile(
        r"fetchDashboard(?:Json|Ndjson)(?:<[^>]+>)?\(\s*([`\"'])(/[^`\"']+)\1",
        re.MULTILINE,
    )
    for match in fetch_pattern.finditer(source):
        path = match.group(2)
        path = re.sub(r"\$\{(?:encodeURIComponent\(guildId\)|encodedGuildId)\}", ":guildId", path)
        path = path.split("?", 1)[0]
        if not path.startswith("/api"):
            path = f"/api{path}"
        paths.add(path)
    for match in re.finditer(r"window\.location\.assign\(\s*([`\"'])(/api/[^`\"']+)\1", source):
        paths.add(match.group(2).split("?", 1)[0])
    return paths


def _backend_registered_paths() -> set[str]:
    paths: set[str] = set()
    route_pattern = re.compile(r"app\.(?:get|post|patch|put|delete)\(\s*([\"'])(/[^\"']+)\1")
    for path in (BACKEND / "src/routes").glob("*.ts"):
        source = path.read_text(encoding="utf-8")
        paths.update(match.group(2) for match in route_pattern.finditer(source))
    return paths


def test_frontend_transport_endpoints_are_registered_by_backend() -> None:
    transport_sources = [
        FRONTEND / "src/transport/dashboardApi.ts",
        FRONTEND / "src/transport/sessionApi.ts",
    ]
    frontend_paths: set[str] = set()
    for path in transport_sources:
        frontend_paths.update(_frontend_transport_paths(path.read_text(encoding="utf-8")))

    backend_paths = _backend_registered_paths()
    assert frontend_paths, "nenhum endpoint do frontend foi detectado"
    missing = sorted(frontend_paths - backend_paths)
    assert missing == [], f"frontend chama endpoints sem rota correspondente no backend: {missing}"

    dashboard_api = (FRONTEND / "src/transport/dashboardApi.ts").read_text(encoding="utf-8")
    backend_mutations = (BACKEND / "src/routes/dashboardApiMutationRoutes.ts").read_text(encoding="utf-8")
    assert 'method: "PATCH"' in dashboard_api
    assert 'app.patch("/api/dashboard/guild/:guildId/settings"' in backend_mutations


def test_select_and_account_popup_mechanics_are_extracted() -> None:
    smart = (FRONTEND / "src/components/SmartSelect.tsx").read_text(encoding="utf-8")
    account = (FRONTEND / "src/components/AccountMenu.tsx").read_text(encoding="utf-8")
    assert "useSmartSelect" in smart
    assert "useAccountMenu" in account
    assert "addEventListener(\"keydown\"" not in smart
    assert "addEventListener(\"keydown\"" not in account
    assert "smartSelectPosition" in (FRONTEND / "src/components/smart-select/useSmartSelect.ts").read_text(encoding="utf-8")
    assert "accountMenuPosition" in (FRONTEND / "src/components/account-menu/useAccountMenu.ts").read_text(encoding="utf-8")


def test_full_dashboard_payload_is_composed_outside_routes() -> None:
    routes = (BACKEND / "src/routes/dashboardApiRoutes.ts").read_text(encoding="utf-8")
    read_routes = (BACKEND / "src/routes/dashboardApiReadRoutes.ts").read_text(encoding="utf-8")
    loader = (BACKEND / "src/services/dashboardFullLoader.ts").read_text(encoding="utf-8")
    assert "registerDashboardApiReadRoutes" in routes
    assert "registerDashboardApiMutationRoutes" in routes
    assert "createDashboardFullLoader" in read_routes
    assert "Promise.all" not in read_routes
    assert "Promise.all" in loader
    assert 'track("settings"' in loader
    assert 'track("summary"' in loader
    assert 'track("options"' in loader
    assert 'track("bot"' in loader


def test_api_routes_are_composed_from_read_and_mutation_modules() -> None:
    facade = (BACKEND / "src/routes/dashboardApiRoutes.ts").read_text(encoding="utf-8")
    read_routes = BACKEND / "src/routes/dashboardApiReadRoutes.ts"
    mutation_routes = BACKEND / "src/routes/dashboardApiMutationRoutes.ts"
    route_types = BACKEND / "src/routes/dashboardApiRouteTypes.ts"
    assert "app.get(" not in facade
    assert "app.patch(" not in facade
    assert read_routes.is_file() and len(read_routes.read_text(encoding="utf-8").splitlines()) <= 210
    assert mutation_routes.is_file() and len(mutation_routes.read_text(encoding="utf-8").splitlines()) <= 80
    assert route_types.is_file()


def test_session_service_is_a_thin_facade_over_engine_and_store() -> None:
    service = (BACKEND / "src/services/dashboardSessionService.ts").read_text(encoding="utf-8")
    engine = (BACKEND / "src/services/dashboardSessionEngine.ts").read_text(encoding="utf-8")
    store = (BACKEND / "src/services/dashboardSessionStore.ts").read_text(encoding="utf-8")
    types = BACKEND / "src/services/dashboardSessionTypes.ts"
    assert "MongoClient" not in service
    assert "runSingleFlight" not in service
    assert "createDashboardSessionEngine" in service
    assert "createMongoDashboardSessionStore" in service
    assert "runSingleFlight" in engine
    assert "MongoClient" in store
    assert types.is_file()
    assert len(engine.splitlines()) <= 180
    assert len(store.splitlines()) <= 130


def test_discord_auth_facade_keeps_public_contract_while_delegating() -> None:
    facade = (BACKEND / "src/services/discordAuthService.ts").read_text(encoding="utf-8")
    assert "fetchDiscordJson" not in facade
    assert "process.env" not in facade
    for module in ("discordIdentityService", "discordAccessService", "discordAuthConfig", "discordPermissions", "discordPresentation"):
        assert module in facade
    engine = (BACKEND / "src/services/dashboardSessionEngine.ts").read_text(encoding="utf-8")
    assert "runSingleFlight" in engine
    assert "void flight.then" not in engine



def test_discord_access_facade_delegates_domain_responsibilities() -> None:
    facade = (BACKEND / "src/services/discordAccessService.ts").read_text(encoding="utf-8")
    assert "discordGuildAccess" in facade
    assert "discordGuildOptionsService" in facade
    assert "discordServerDiscovery" in facade
    assert "fetchDiscordJson" not in facade
    guild_access = (BACKEND / "src/services/discordGuildAccess.ts").read_text(encoding="utf-8")
    discovery = (BACKEND / "src/services/discordServerDiscovery.ts").read_text(encoding="utf-8")
    options = (BACKEND / "src/services/discordGuildOptionsService.ts").read_text(encoding="utf-8")
    assert "verifyDashboardAccess" in guild_access
    assert "listDashboardServers" in discovery
    assert "listGuildChannelsAndRoles" in options

def test_message_preview_facades_delegate_to_bounded_variant_modules() -> None:
    variants = (FRONTEND / "src/components/message-editor/MessagePreviewVariants.tsx").read_text(encoding="utf-8")
    primitives = (FRONTEND / "src/components/message-editor/MessagePreviewPrimitives.tsx").read_text(encoding="utf-8")
    for module in ("MessagePreviewEmbedVariants", "MessagePreviewV2Variants", "MessagePreviewGenericVariant", "ColorRolesPanelPreview"):
        assert module in variants
    for module in ("MessagePreviewEditablePrimitives", "MessagePreviewMediaPrimitives", "MessagePreviewAccentControl"):
        assert module in primitives
    assert "export function" not in variants
    assert "export function" not in primitives


def test_message_editor_utils_is_a_domain_facade() -> None:
    utils = (FRONTEND / "src/components/message-editor/messageEditorUtils.ts").read_text(encoding="utf-8")
    assert "messageEditorJson" in utils
    assert "messageEditorMedia" in utils
    assert "messageEditorTemplate" in utils
    assert "function " not in utils


def _ordered_route_literals(path: Path) -> list[tuple[str, str]]:
    source = path.read_text(encoding="utf-8")
    routes: list[tuple[int, str, str]] = []
    for match in re.finditer(r"app\.(get|post|patch|put|delete)\(\s*([\"'])(/[^\"']+)\2", source):
        routes.append((match.start(), match.group(1).upper(), match.group(3)))
    for match in re.finditer(r"app\.(get|all)\(\s*\[([^\]]+)\]", source):
        method = match.group(1).upper()
        for path_match in re.finditer(r"[\"'](/[^\"']+)[\"']", match.group(2)):
            routes.append((match.start() + path_match.start(), method, path_match.group(1)))
    return [(method, route) for _, method, route in sorted(routes)]


def test_dashboard_route_registration_order_remains_stable() -> None:
    ordered = [
        *_ordered_route_literals(BACKEND / "src/routes/dashboardHealthRoute.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/dashboardOAuthRoutes.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/dashboardPublicIdentityRoute.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/dashboardSessionRoutes.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/dashboardApiReadRoutes.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/dashboardApiMutationRoutes.ts"),
        *_ordered_route_literals(BACKEND / "src/routes/registerDashboardRoutes.ts"),
    ]
    assert ordered == [
        ("GET", "/health"),
        ("GET", "/api/health"),
        ("GET", "/api/auth/login"),
        ("GET", "/api/auth/callback"),
        ("GET", "/api/public/identity"),
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/dashboard/tts/voices"),
        ("GET", "/api/dashboard/servers"),
        ("GET", "/api/dashboard/guild/:guildId/invite"),
        ("GET", "/api/dashboard/bootstrap"),
        ("GET", "/api/dashboard/guild/:guildId/full"),
        ("GET", "/api/dashboard/guild/:guildId/full-progress"),
        ("GET", "/api/dashboard/guild/:guildId/summary"),
        ("GET", "/api/dashboard/guild/:guildId/settings"),
        ("GET", "/api/dashboard/guild/:guildId/options"),
        ("GET", "/api/dashboard/guild/:guildId/commands"),
        ("GET", "/api/dashboard/media-preview"),
        ("PATCH", "/api/dashboard/guild/:guildId/settings"),
        ("ALL", "/token"),
        ("ALL", "/api/token"),
        ("ALL", "/session"),
        ("ALL", "/api/session"),
    ]



def test_dashboard_auth_facade_composes_bounded_routes_and_preserves_guards() -> None:
    facade = (BACKEND / "src/routes/dashboardAuthRoutes.ts").read_text(encoding="utf-8")
    assert "registerDashboardHealthRoute(options.app)" in facade
    assert "registerDashboardOAuthRoutes(options)" in facade
    assert "registerDashboardPublicIdentityRoute(options.app)" in facade
    assert "registerDashboardSessionRoutes(options)" in facade
    assert "requireDashboardAccess" in facade and "requireSession" in facade
    guard = (BACKEND / "src/routes/dashboardAuthGuard.ts").read_text(encoding="utf-8")
    assert "session_store_unavailable" in guard
    assert "session_invalid" in guard
    oauth = (BACKEND / "src/routes/dashboardOAuthRoutes.ts").read_text(encoding="utf-8")
    assert "buildDiscordOAuthAuthorizeUrl" in oauth
    assert "oauth_exchange_failed" in oauth


def test_message_editor_dialog_lifecycle_is_composed_from_bounded_concerns() -> None:
    lifecycle = (FRONTEND / "src/components/message-editor/useMessageEditorDialogLifecycle.ts").read_text(encoding="utf-8")
    close = (FRONTEND / "src/components/message-editor/useMessageEditorDialogClose.ts").read_text(encoding="utf-8")
    environment = (FRONTEND / "src/components/message-editor/useMessageEditorDialogEnvironment.ts").read_text(encoding="utf-8")
    assert "useMessageEditorDialogClose" in lifecycle
    assert "useMessageEditorDialogEnvironment" in lifecycle
    assert "window.confirm" in close
    assert "window.history.back" in close
    assert "messageEditorEscapeAction" in environment
    assert "messageEditorViewportHeight" in environment
    assert 'addEventListener("keydown"' in environment
    assert "document.body.style.position" in environment

def test_dashboard_commands_facade_keeps_worker_detection_isolated() -> None:
    service = (BACKEND / "src/services/dashboardCommandsService.ts").read_text(encoding="utf-8")
    worker = (BACKEND / "src/services/dashboardWorkerAvailability.ts").read_text(encoding="utf-8")
    model = (BACKEND / "src/services/dashboardCommandsModel.ts").read_text(encoding="utf-8")
    assert "dashboardWorkerAvailable" in service
    assert "buildDashboardCommandsPayload" in service
    assert "core_workers_registry" not in service
    assert "requiredCapabilities" in worker
    assert "CORE_WORKERS_REGISTRY_PATH" in worker
    assert '"music"' not in worker
    assert 'cogs/musica/site/dashboard-worker.json' in service
    assert "sourceCategory === \"music\" && !musicAvailable" in model


def test_message_visual_editor_keeps_contextual_select_mechanics_isolated() -> None:
    editor = (FRONTEND / "src/components/message-editor/MessageVisualEditor.tsx").read_text(encoding="utf-8")
    select = (FRONTEND / "src/components/message-editor/MessageContextualSelect.tsx").read_text(encoding="utf-8")
    model = (FRONTEND / "src/components/message-editor/messageVisualEditorModel.ts").read_text(encoding="utf-8")
    assert "MessageContextualSelect" in editor
    assert "createPortal" not in editor
    assert "addEventListener" not in editor
    assert "createPortal" in select
    assert "visualViewport" in select
    assert "contextualMessageOptions" in editor and "contextualMessageOptions" in model


def test_default_and_value_codec_facades_delegate_by_domain() -> None:
    defaults = (BACKEND / "src/config/dashboardDocumentDefaults.ts").read_text(encoding="utf-8")
    codec = (BACKEND / "src/config/dashboardValueCodec.ts").read_text(encoding="utf-8")
    assert len(defaults.splitlines()) <= 12
    for module in (
        "dashboardColorRoleDefaults",
        "dashboardFormsDefaults",
        "dashboardGuildDefaults",
        "dashboardFeatureDefaults",
    ):
        assert module in defaults
    assert "dashboardColorValueCodec" in codec
    assert "dashboardFormValueCodec" in codec
    assert "function cleanColor" not in codec
    assert "function normalizeFormFields" not in codec
    assert "function normalizeColorSlots" not in codec


def test_dashboard_config_service_is_composed_from_repository_and_pure_model() -> None:
    service = (BACKEND / "src/services/dashboardConfigService.ts").read_text(encoding="utf-8")
    model = (BACKEND / "src/services/dashboardConfigModel.ts").read_text(encoding="utf-8")
    repository = (BACKEND / "src/services/dashboardConfigRepository.ts").read_text(encoding="utf-8")
    assert "MongoClient" not in service
    assert "createDashboardConfigRepository" in service
    assert "planDashboardUpdates" in service
    assert "MongoClient" in repository
    assert "collection.updateOne" in repository
    assert "DashboardConfigValidationError" in model
    assert "webhook.channel_id = channelId" in model
    assert 'Number(getPath(docs.welcome, "channel_id")' not in model


def test_app_route_lifecycle_effects_are_extracted() -> None:
    app = (FRONTEND / "src/App.tsx").read_text(encoding="utf-8")
    effects = (FRONTEND / "src/app/useDashboardRouteEffects.ts").read_text(encoding="utf-8")
    assert "useDashboardRouteEffects" in app
    assert "useEffect(" not in app
    assert "loadServers" in effects
    assert "loadDashboard" in effects
    assert "Esse módulo não existe mais" in effects
    assert "setSelectedServer" in effects

def test_frontend_npm_install_includes_build_tooling() -> None:
    npmrc = (FRONTEND / ".npmrc").read_text(encoding="utf-8")
    entries = {
        line.strip().lower()
        for line in npmrc.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    }
    assert "include=dev" in entries, (
        "dashboard/frontend/.npmrc deve manter include=dev para que o updater "
        "instale tsx/typescript/vite antes dos testes e do build"
    )
    assert "engine-strict=false" in entries, (
        "dashboard/frontend/.npmrc deve neutralizar engine-strict global durante "
        "a instalação transacional do updater"
    )
    assert "fetch-retries=5" in entries
    assert "fetch-timeout=120000" in entries



def test_frontend_lock_is_explicit_and_migration_normalizes_it_with_local_npm() -> None:
    lock = ROOT / "dashboard" / "frontend" / "package-lock.json"
    assert lock.is_file(), "package-lock canônico deve viajar no pacote de recuperação"
    source = (ROOT / "scripts" / "migrate-dashboard-layout.sh").read_text(encoding="utf-8")
    assert "npm install --package-lock-only --ignore-scripts --include=dev" in source
    assert 'source.name == "package-lock.json"' in source
