from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "updater" / "utilitarios" / "selecao_testes.py"
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _select(project: Path, prefix: str, changed: str) -> dict[str, object]:
    env = os.environ.copy()
    env["CHANGED_FILES_RAW_INPUT"] = changed
    result = subprocess.run(
        ["python3", str(SELECTOR), "--project", str(project), "--prefix", prefix],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_test_only_runs_exact_changed_frontend_test() -> None:
    plan = _select(
        ROOT / "dashboard" / "frontend",
        "dashboard/frontend",
        "dashboard/frontend/tests/app-model.test.ts",
    )
    assert plan["mode"] == "selected"
    assert plan["tests"] == ["tests/app-model.test.ts"]
    assert plan["reason"] == "changed-tests-only"


def test_frontend_source_selects_transitive_test_instead_of_full_suite() -> None:
    plan = _select(
        ROOT / "dashboard" / "frontend",
        "dashboard/frontend",
        "dashboard/frontend/src/app/appModel.ts",
    )
    assert plan["mode"] == "selected"
    assert plan["tests"] == ["tests/app-model.test.ts"]
    assert plan["selected_count"] == 1
    assert plan["total_count"] >= 20


def test_backend_source_selects_all_transitive_dependents() -> None:
    plan = _select(
        ROOT / "dashboard" / "backend",
        "dashboard/backend",
        "dashboard/backend/src/services/singleFlight.ts",
    )
    assert plan["mode"] == "selected"
    assert plan["tests"] == [
        "tests/dashboard-session-engine.test.ts",
        "tests/single-flight.test.ts",
    ]


def test_global_project_file_falls_back_to_full_suite() -> None:
    plan = _select(
        ROOT / "dashboard" / "frontend",
        "dashboard/frontend",
        "dashboard/frontend/package-lock.json",
    )
    assert plan["mode"] == "full"
    assert str(plan["reason"]).startswith("global-path:")


def test_deleted_source_falls_back_to_full_suite(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tests" / "thing.test.ts").write_text("import '../src/thing'\n", encoding="utf-8")
    plan = _select(project, "dashboard/frontend", "dashboard/frontend/src/thing.ts")
    assert plan == {"mode": "full", "reason": "deleted-source:src/thing.ts", "tests": []}


def test_unresolved_relative_code_import_falls_back_to_full_suite(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "thing.ts").write_text("export const x = 1\n", encoding="utf-8")
    (project / "src" / "other.ts").write_text("import './missing'\nexport const y = 2\n", encoding="utf-8")
    (project / "tests" / "thing.test.ts").write_text("import '../src/thing'\n", encoding="utf-8")
    plan = _select(project, "dashboard/frontend", "dashboard/frontend/src/thing.ts")
    assert plan["mode"] == "full"
    assert str(plan["reason"]).startswith("unresolved-import:")


def test_updater_executes_selected_test_command_and_reports_plan() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    block = source[start:end]
    assert 'select_node_test_plan "$front_dir" "dashboard/frontend"' in block
    assert 'select_node_test_plan "$back_dir" "dashboard/backend"' in block
    assert 'cd \\"$front_dir\\" && $front_test_command' in block
    assert 'cd \\"$back_dir\\" && $back_test_command' in block
    assert "Testes: $TEST_PLAN_TEXT" in source


def _health_profile(changed: str, tmp_path: Path) -> str:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("bot_health_profile_for_changed_files() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    fragment = tmp_path / "profile.sh"
    fragment.write_text(source[start:end], encoding="utf-8")
    env = os.environ.copy()
    env["CHANGED"] = changed
    result = subprocess.run(
        [
            "bash",
            "-eu",
            "-o",
            "pipefail",
            "-c",
            f'source "{fragment}"; CHANGED_FILES_RAW="$CHANGED"; bot_health_profile_for_changed_files',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def test_bot_health_profile_is_shorter_only_for_proven_scoped_changes(tmp_path: Path) -> None:
    assert _health_profile("cogs/ping.py", tmp_path) == "cogs"
    assert _health_profile("cogs/tts/cog.py", tmp_path) == "cogs"
    assert _health_profile("cogs/tts/cog.py\ntests/test_tts.py", tmp_path) == "cogs"
    assert _health_profile("bot.py", tmp_path) == "critical"
    assert _health_profile("requirements.txt", tmp_path) == "critical"
    assert _health_profile("utility/foo.py", tmp_path) == "standard"


def test_health_profiles_preserve_environment_overrides_and_reload_fast_path() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("verify_bot_after_restart() {")
    end = source.index("\nis_placeholder_status_text() {", start)
    block = source[start:end]
    assert 'reload) default_stability=3' in block
    assert 'cogs) default_stability=5' in block
    assert 'critical) default_stability=10' in block
    assert 'UPDATE_BOT_HEALTH_STABILITY_SECONDS:-$default_stability' in block
    assert 'verify_bot_after_restart "$verification_epoch" "$restarts_before" 0 reload' in source
    assert 'health_profile="$(bot_health_profile_for_changed_files)"' in source


def test_backend_healthcheck_uses_adaptive_polling() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert "wait_for_health_adaptive() {" in source
    backend = source[source.index("deploy_backend() {") : source.index("\nruntime_release_root_for_commit() {")]
    assert 'wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2' in backend
    assert 'wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2' in backend
    assert 'declare -F wait_for_health_adaptive' in backend


def _adaptive_health_fragment(tmp_path: Path) -> Path:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("wait_for_health_adaptive() {")
    end = source.index("\nfetch_bot_health_json() {", start)
    fragment = tmp_path / "adaptive-health.sh"
    fragment.write_text(source[start:end], encoding="utf-8")
    return fragment


def test_adaptive_health_does_not_sleep_when_service_is_already_ready(tmp_path: Path) -> None:
    fragment = _adaptive_health_fragment(tmp_path)
    harness = f'''
source "{fragment}"
curl() {{ return 0; }}
sleep() {{ echo "UNEXPECTED_SLEEP:$1"; return 77; }}
wait_for_health_adaptive http://127.0.0.1:8787/health 14 2
printf 'READY\n'
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "READY"


def test_adaptive_health_uses_subsecond_backoff_before_success(tmp_path: Path) -> None:
    fragment = _adaptive_health_fragment(tmp_path)
    harness = f'''
source "{fragment}"
count=0
curl() {{ count=$((count+1)); (( count >= 3 )); }}
sleep() {{ printf 'SLEEP=%s\n' "$1"; }}
wait_for_health_adaptive http://127.0.0.1:8787/health 14 2
'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["SLEEP=0.20", "SLEEP=0.40"]
