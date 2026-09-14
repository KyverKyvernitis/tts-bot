from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-Eeuo", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )


def test_priority_profiles_switch_once_and_use_expected_levels(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("set_updater_priority_profile() {")
    end = source.index("\n\nset_updater_priority_profile safe", start)
    helper = tmp_path / "priority.sh"
    helper.write_text(source[start:end] + "\n", encoding="utf-8")
    calls = tmp_path / "calls.log"

    result = _run_bash(
        f'''source "{helper}"
UPDATER_NICE_LEVEL=10
UPDATER_IONICE_CLASS=2
UPDATER_IONICE_LEVEL=7
UPDATER_FAST_NICE_LEVEL=5
UPDATER_FAST_IONICE_CLASS=2
UPDATER_FAST_IONICE_LEVEL=4
UPDATER_PRIORITY_PROFILE=''
LOG_TAG=test
renice() {{ printf 'renice %s\\n' "$*" >> "{calls}"; }}
ionice() {{ printf 'ionice %s\\n' "$*" >> "{calls}"; }}
logger() {{ :; }}
set_updater_priority_profile fast
set_updater_priority_profile fast
set_updater_priority_profile safe
printf 'PROFILE=%s\\n' "$UPDATER_PRIORITY_PROFILE"
cat "{calls}"
'''
    )
    assert "PROFILE=safe" in result.stdout
    assert result.stdout.count("renice ") == 2
    assert result.stdout.count("ionice ") == 2
    assert "renice -n 5 -p " in result.stdout
    assert "ionice -c 2 -n 4 -p " in result.stdout
    assert "renice -n 10 -p " in result.stdout
    assert "ionice -c 2 -n 7 -p " in result.stdout


def test_local_candidate_uses_fast_only_for_light_preparation_and_promotion() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_update() {")
    end = source.index("\npublish_local_candidate_after_validation() {", start)
    block = source[start:end]

    assert block.index("set_updater_priority_profile fast") < block.index('repo_git fetch origin "$BRANCH"')
    runtime = block.index('STAGE="preparação de artefatos no worktree"')
    assert block.index("set_updater_priority_profile safe", runtime) < block.index(
        "prepare_local_candidate_runtime_artifacts_in_worktree", runtime
    )
    snapshot = block.index('STAGE="preservação do runtime anterior"')
    assert block.rfind("set_updater_priority_profile safe", 0, snapshot) != -1
    promotion = block.index('zip_progress_done_and_publish "Candidato READY em isolamento" "Promovendo para a VPS"')
    fast_promotion = block.index("set_updater_priority_profile fast", promotion)
    promote_call = block.index("promote_local_candidate_worktree_commit", fast_promotion)
    safe_after = block.index("set_updater_priority_profile safe", promote_call)
    assert promotion < fast_promotion < promote_call < safe_after


def test_remote_candidate_keeps_build_and_snapshot_safe_but_merge_fast() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("  REMOTE_CANDIDATE_MODE=1")
    end = source.index("\nfi\n\nFAILED_STAGE=", start)
    block = source[start:end]

    assert block.index("set_updater_priority_profile fast") < block.index("load_git_diff_snapshot")
    runtime = block.index('STAGE="preparação de artefatos do commit remoto"')
    assert block.index("set_updater_priority_profile safe", runtime) < block.index(
        "prepare_local_candidate_runtime_artifacts_in_worktree", runtime
    )
    snapshot = block.index('STAGE="preservação do runtime anterior"')
    assert block.rfind("set_updater_priority_profile safe", 0, snapshot) != -1
    merge_stage = block.index('STAGE="aplicação do commit GitHub"')
    fast_merge = block.index("set_updater_priority_profile fast", merge_stage)
    merge = block.index('repo_git merge --ff-only "$REMOTE_COMMIT"', fast_merge)
    safe_after = block.index("set_updater_priority_profile safe", merge)
    assert merge_stage < fast_merge < merge < safe_after


def test_error_handler_restores_conservative_priority_before_recovery() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("on_error() {")
    end = source.index("\n}\n\ntrap 'cleanup_runtime_artifacts' EXIT", start)
    block = source[start:end]
    assert block.index('local exit_code="$?"') < block.index("set_updater_priority_profile safe || true")
    assert block.index("set_updater_priority_profile safe || true") < block.index("persist_primary_failure")
