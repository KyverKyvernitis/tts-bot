from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _classify_functions(source: str) -> str:
    start = source.index("classify_changed_files() {")
    end = source.index("\nfast_reload_modules_for_changed_files() {", start)
    return source[start:end]


def _classify(tmp_path: Path, changed: str) -> dict[str, int]:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "classify.sh"
    functions.write_text(_classify_functions(source), encoding="utf-8")
    harness = f'''
source "{functions}"
CHANGED_FILES_RAW="$TEST_CHANGED_FILES"
classify_changed_files
printf 'FRONT=%s FRONT_TESTS=%s BACK=%s BACK_TESTS=%s\n' "$FRONT_CHANGED" "$FRONT_TESTS_CHANGED" "$BACK_CHANGED" "$BACK_TESTS_CHANGED"
'''
    import os
    env = os.environ.copy()
    env["TEST_CHANGED_FILES"] = changed
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    values: dict[str, int] = {}
    for token in result.stdout.strip().split():
        key, raw = token.split("=", 1)
        values[key] = int(raw)
    return values


def test_frontend_test_only_does_not_mark_runtime_for_publish(tmp_path: Path) -> None:
    values = _classify(tmp_path, "dashboard/frontend/tests/app.test.ts")
    assert values == {"FRONT": 0, "FRONT_TESTS": 1, "BACK": 0, "BACK_TESTS": 0}


def test_backend_test_only_does_not_mark_runtime_for_restart(tmp_path: Path) -> None:
    values = _classify(tmp_path, "dashboard/backend/tests/api.test.ts")
    assert values == {"FRONT": 0, "FRONT_TESTS": 0, "BACK": 0, "BACK_TESTS": 1}


def test_source_plus_tests_keeps_runtime_publish_required(tmp_path: Path) -> None:
    values = _classify(
        tmp_path,
        "dashboard/frontend/tests/app.test.ts\ndashboard/frontend/src/App.tsx\n"
        "dashboard/backend/tests/api.test.ts\ndashboard/backend/src/index.ts",
    )
    assert values == {"FRONT": 1, "FRONT_TESTS": 1, "BACK": 1, "BACK_TESTS": 1}


def test_unknown_dashboard_path_is_conservatively_runtime_relevant(tmp_path: Path) -> None:
    values = _classify(tmp_path, "dashboard/frontend/README.md\ndashboard/backend/notes.txt")
    assert values["FRONT"] == 1
    assert values["BACK"] == 1


def test_test_only_paths_run_validation_but_skip_build_artifact() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    block = source[start:end]

    assert "if (( FRONT_CHANGED == 1 || ${FRONT_TESTS_CHANGED:-0} == 1 )); then" in block
    assert "if (( BACK_CHANGED == 1 || ${BACK_TESTS_CHANGED:-0} == 1 )); then" in block
    assert 'FRONT_STATUS="testes do frontend aprovados; runtime não alterado"' in block
    assert 'BACK_STATUS="testes do backend aprovados; runtime não alterado"' in block


def test_deploy_reports_test_only_without_publication_or_restart() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    frontend = source[source.index("deploy_frontend() {") : source.index("\ninstall_backend_prebuilt_artifact() {")]
    backend = source[source.index("deploy_backend() {") : source.index("\nruntime_release_root_for_commit() {")]

    assert '${FRONT_TESTS_CHANGED:-0} == 1' in frontend
    assert "publicação não necessária" in frontend
    assert '${BACK_TESTS_CHANGED:-0} == 1' in backend
    assert "publicação não necessária" in backend
