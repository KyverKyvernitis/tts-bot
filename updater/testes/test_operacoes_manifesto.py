from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from updater.testes.fonte_core import caminho_fonte_core
from updater.testes.fonte_discord import ler_fonte_discord


ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()
BOT = ROOT / "bot.py"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    (repo / "cogs").mkdir(parents=True)
    (repo / "cogs" / "obsolete.py").write_text("OLD = 1\n", encoding="utf-8")
    (repo / "cogs" / "before.py").write_text("MOVE = 1\n", encoding="utf-8")
    (repo / "cogs" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Updater Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)


def test_schema_v3_applies_delete_move_add_update_and_stages_exact_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    files = candidate / "files" / "cogs"
    _init_repo(repo)
    files.mkdir(parents=True)
    (files / "keep.py").write_text("VALUE = 2\n", encoding="utf-8")
    (files / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    operations = [
        {"op": "delete", "path": "cogs/obsolete.py"},
        {"op": "move", "from": "cogs/before.py", "to": "cogs/after.py"},
        {"op": "update", "path": "cogs/keep.py"},
        {"op": "add", "path": "cogs/new.py"},
    ]
    changed = ["cogs/obsolete.py", "cogs/before.py", "cogs/after.py", "cogs/keep.py", "cogs/new.py"]
    (candidate / "manifest.json").write_text(
        json.dumps({"schema_version": 3, "changed_files": changed, "operations": operations}),
        encoding="utf-8",
    )

    harness = f"""
source <(awk '/^refresh_changed_files_from_staged_diff[(][)]/{{flag=1}} /^apply_local_candidate_patch_diff[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^apply_local_candidate_operations[(][)]/{{flag=1}} /^copy_local_candidate_files[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^copy_local_candidate_files[(][)]/{{flag=1}} /^normalize_changed_file_permissions[(][)]/{{flag=0}} flag' {UPDATER!s})
source <(awk '/^git_add_changed_files[(][)]/{{flag=1}} /^prepare_local_candidate_update[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
REPO_DIR={repo!s}
LOCAL_CANDIDATE_SCHEMA_VERSION=3
LOCAL_CANDIDATE_DIR={candidate!s}
LOCAL_CANDIDATE_FILES_DIR={candidate / 'files'!s}
CHANGED_FILES_RAW=$'cogs/obsolete.py\\ncogs/before.py\\ncogs/after.py\\ncogs/keep.py\\ncogs/new.py'
CHANGED_STATUS_RAW=''
CHANGED_DIFF_NUMSTAT_RAW=''
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
LOG_TAG='test-updater'
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_repo_dir() {{ printf '%s\n' "$REPO_DIR"; }}
candidate_git() {{ git -C "$(candidate_repo_dir)" "$@"; }}
apply_local_candidate_operations
# Retomar o mesmo candidato deve ser idempotente para delete/move já staged.
apply_local_candidate_operations
copy_local_candidate_files
git_add_changed_files
refresh_changed_files_from_staged_diff
printf '%s\\n' "$CHANGED_STATUS_RAW"
printf '%s\\n' '---FILES---'
printf '%s\\n' "$CHANGED_FILES_RAW"
"""
    result = _run_bash(harness)
    output = result.stdout
    assert "D\tcogs/obsolete.py" in output
    assert "D\tcogs/before.py" in output
    assert "A\tcogs/after.py" in output
    assert "M\tcogs/keep.py" in output
    assert "A\tcogs/new.py" in output
    assert not (repo / "cogs" / "obsolete.py").exists()
    assert not (repo / "cogs" / "before.py").exists()
    assert (repo / "cogs" / "after.py").read_text(encoding="utf-8") == "MOVE = 1\n"
    assert (repo / "cogs" / "keep.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_new_candidates_do_not_trigger_filename_magic_migration() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    prepare = source[source.index("prepare_local_candidate_update() {") : source.index("publish_local_candidate_after_validation() {")]
    assert '(( LOCAL_CANDIDATE_SCHEMA_VERSION < 3 )) && [[ -f "$apply_repo/scripts/migrate-dashboard-layout.sh" ]]' in prepare
    assert 'REPO_DIR="$apply_repo" bash "$apply_repo/scripts/migrate-dashboard-layout.sh"' in prepare
    assert "apply_local_candidate_operations" in prepare


def test_bot_emits_schema_v3_and_reserves_control_manifest() -> None:
    source = ler_fonte_discord()
    assert 'UPDATE_CONTROL_MANIFEST_NAME' in source
    assert 'allowed_ops={"delete", "move"}' in source
    assert '"schema_version": 3' in source
    assert '"operations": normalized_operations' in source
    assert 'content_changed_files = update_operation_paths(content_operations)' in source
    assert '["git", "add", "-A", "--", *content_changed_files]' in source
    assert '"--numstat", "--no-renames"' in source


def test_resuming_active_candidate_does_not_burn_attempt() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    load = source[source.index("load_pending_local_candidate() {") : source.index("verify_local_candidate_integrity() {")]
    assert "resuming_active=1" in load
    assert "resuming = os.environ.get('RESUMING_ACTIVE') == '1'" in load
    assert "attempt = max(1, previous_attempt) if resuming else previous_attempt + 1" in load
    assert "'resume_count': resume_count" in load


def test_schema_v3_recovers_partial_filesystem_delete_and_move(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    candidate = tmp_path / "candidate"
    _init_repo(repo)
    candidate.mkdir(parents=True)
    # Simula interrupção entre mutação do worktree e stage.
    (repo / "cogs" / "obsolete.py").unlink()
    (repo / "cogs" / "before.py").rename(repo / "cogs" / "after.py")
    operations = [
        {"op": "delete", "path": "cogs/obsolete.py"},
        {"op": "move", "from": "cogs/before.py", "to": "cogs/after.py"},
    ]
    (candidate / "manifest.json").write_text(
        json.dumps({"schema_version": 3, "operations": operations}),
        encoding="utf-8",
    )
    harness = f"""
source <(awk '/^apply_local_candidate_operations[(][)]/{{flag=1}} /^copy_local_candidate_files[(][)]/{{flag=0}} flag' {UPDATER!s})
sudo() {{
  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi
  [[ "${{1:-}}" == '-H' ]] && shift
  "$@"
}}
REPO_DIR={repo!s}
LOCAL_CANDIDATE_SCHEMA_VERSION=3
LOCAL_CANDIDATE_DIR={candidate!s}
LAST_ERROR_STDERR=''
LAST_ERROR_CODE=''
repo_git() {{ git -C "$REPO_DIR" "$@"; }}
candidate_repo_dir() {{ printf '%s\n' "$REPO_DIR"; }}
candidate_git() {{ git -C "$(candidate_repo_dir)" "$@"; }}
apply_local_candidate_operations
git -C "$REPO_DIR" diff --cached --name-status --no-renames
"""
    result = _run_bash(harness)
    assert "D\tcogs/obsolete.py" in result.stdout
    assert "D\tcogs/before.py" in result.stdout
    assert "A\tcogs/after.py" in result.stdout



@pytest.fixture
def preparation_mixin(monkeypatch):
    import sys
    import types
    import importlib.util
    import logging

    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace())
    package = types.ModuleType("updater.discord")
    package.__path__ = [str(ROOT / "updater" / "discord")]
    monkeypatch.setitem(sys.modules, "updater.discord", package)
    constantes = types.ModuleType("updater.discord.constantes")
    constantes.UPDATE_LOG = logging.getLogger("zip_update-test")
    monkeypatch.setitem(sys.modules, "updater.discord.constantes", constantes)
    spec = importlib.util.spec_from_file_location(
        "updater.discord.preparacao", ROOT / "updater" / "discord" / "preparacao.py"
    )
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "updater.discord.preparacao", modulo)
    spec.loader.exec_module(modulo)
    return modulo.PreparacaoUpdaterMixin


@pytest.mark.parametrize("missing_paths", [[], ["deploy/voicepeak-teto/optional.py", "missing/file.py"]])
def test_discord_delete_only_does_not_restage_removed_pathspec(
    tmp_path: Path, preparation_mixin, missing_paths: list[str]
) -> None:
    """`git rm` stageia delete; ausências opcionais não entram no candidato."""
    import zipfile

    remote = tmp_path / "remote.git"
    live = tmp_path / "live"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(live)], check=True)
    subprocess.run(["git", "-C", str(live), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(live), "config", "user.name", "Updater Test"], check=True)
    (live / "deploy" / "scripts").mkdir(parents=True)
    doomed = live / "deploy" / "scripts" / "tts-bot-update.sh"
    doomed.write_text("#!/bin/sh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(live), "add", "."], check=True)
    subprocess.run(["git", "-C", str(live), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(live), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(live), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(live), "push", "-q", "-u", "origin", "main"], check=True)

    package = tmp_path / "delete-only.zip"
    manifest = {
        "schema_version": 1,
        "operations": [
            {"op": "delete", "path": "deploy/scripts/tts-bot-update.sh"},
            *({"op": "delete", "path": rel} for rel in missing_paths),
        ],
    }
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("update-manifest.json", json.dumps(manifest))

    class Harness(preparation_mixin):
        def __init__(self) -> None:
            self._repo_root = live
            self._update_temp_root = tmp_path / "tmp"
            self._update_staging_root = tmp_path / "staging"

        def _zip_update_limits(self):
            from updater.utilitarios.seguranca import ZipLimits
            return ZipLimits()

        def _phone_worker_validate_zip_sync(self, _zip_path):
            return None

        def _write_local_update_candidate_sync(self, **kwargs):
            assert kwargs["changed_files"] == ["deploy/scripts/tts-bot-update.sh"]
            assert kwargs["operations"] == [
                {"op": "delete", "path": "deploy/scripts/tts-bot-update.sh"}
            ]
            return {
                "candidate_id": "test",
                "display_id": "TEST",
                "candidate_dir": str(tmp_path / "candidate"),
                "queue_position": 1,
                "queue_pending_count": 1,
                "candidate_prepare_elapsed_ms": 0,
            }

    result = Harness()._process_zip_update_sync(package)
    assert result["changed_files"] == ["deploy/scripts/tts-bot-update.sh"]
    staging = Path(result["staging_dir"])
    status = subprocess.run(
        ["git", "-C", str(staging), "diff", "--cached", "--name-status", "--no-renames"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "D\tdeploy/scripts/tts-bot-update.sh" in status


def test_discord_absent_delete_only_produces_no_candidate(tmp_path: Path, preparation_mixin) -> None:
    import zipfile

    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)], check=True)
    package = tmp_path / "already-clean.zip"
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr(
            "update-manifest.json",
            json.dumps({"schema_version": 1, "operations": [{"op": "delete", "path": "missing/file.py"}]}),
        )

    class Harness(preparation_mixin):
        _repo_root = repo
        _update_temp_root = tmp_path / "tmp"
        _update_staging_root = tmp_path / "staging"

        def _zip_update_limits(self):
            from updater.utilitarios.seguranca import ZipLimits
            return ZipLimits()

        def _phone_worker_validate_zip_sync(self, _zip_path):
            return None

        def _write_local_update_candidate_sync(self, **kwargs):
            pytest.fail("Um delete ausente não deve gerar candidato")

    result = Harness()._process_zip_update_sync(package)
    assert result["changed_files"] == []
    assert result["triggered_update"] is False
    assert result["trigger_detail"] == "sem mudanças"


def test_discord_absent_tracked_delete_stages_removal_and_retry_is_empty(
    tmp_path: Path, preparation_mixin
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "cogs" / "obsolete.py").unlink()
    operations = [{"op": "delete", "path": "cogs/obsolete.py"}]
    harness = preparation_mixin()
    assert harness._apply_declarative_operations_to_clone(operations, repo, os.environ.copy()) == operations
    assert harness._apply_declarative_operations_to_clone(operations, repo, os.environ.copy()) == []
    status = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-status", "--no-renames"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == "D\tcogs/obsolete.py\n"


def test_discord_absent_wildcard_delete_cannot_match_other_tracked_files(
    tmp_path: Path, preparation_mixin
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = preparation_mixin()._apply_declarative_operations_to_clone(
        [{"op": "delete", "path": "cogs/*.py"}], repo, os.environ.copy()
    )
    assert result == []
    assert {path.name for path in (repo / "cogs").iterdir()} == {"obsolete.py", "before.py", "keep.py"}
    assert not subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.parametrize("kind", ["untracked", "directory", "symlink", "broken_symlink", "outside_symlink"])
def test_discord_delete_rejects_unsafe_existing_targets(
    tmp_path: Path, preparation_mixin, kind: str
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "cogs" / "unsafe.py"
    if kind == "untracked":
        target.write_text("local data\n", encoding="utf-8")
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to("keep.py")
    elif kind == "broken_symlink":
        target.symlink_to("missing.py")
    else:
        target.symlink_to(tmp_path / "outside.py")
    with pytest.raises(RuntimeError):
        preparation_mixin()._apply_declarative_operations_to_clone(
            [{"op": "delete", "path": "cogs/unsafe.py"}], repo, os.environ.copy()
        )
    assert target.exists() or target.is_symlink()
    assert (repo / "cogs" / "keep.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize("rel", ["../outside.py", ".git/absent", ".env"])
def test_discord_absent_delete_still_rejects_protected_or_outside_paths(
    tmp_path: Path, preparation_mixin, rel: str
) -> None:
    from updater.utilitarios.seguranca import UpdateSecurityError

    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(UpdateSecurityError):
        preparation_mixin()._apply_declarative_operations_to_clone(
            [{"op": "delete", "path": rel}], repo, os.environ.copy()
        )


def test_discord_absent_delete_does_not_hide_git_query_failure(
    tmp_path: Path, preparation_mixin, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    harness = preparation_mixin()

    def failing_git(args, cwd, *, env):
        assert args[2] == "ls-files"
        return subprocess.CompletedProcess(args, 128, "", "fatal: damaged index")

    monkeypatch.setattr(harness, "_run_cmd", failing_git)
    with pytest.raises(RuntimeError, match="damaged index"):
        harness._apply_declarative_operations_to_clone(
            [{"op": "delete", "path": "missing/file.py"}], repo, os.environ.copy()
        )


def test_discord_optional_delete_keeps_actual_move_and_delete_operations(
    tmp_path: Path, preparation_mixin
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    operations = [
        {"op": "delete", "path": "missing/file.py"},
        {"op": "move", "from": "cogs/before.py", "to": "cogs/after.py"},
        {"op": "delete", "path": "cogs/obsolete.py"},
    ]
    assert preparation_mixin()._apply_declarative_operations_to_clone(
        operations, repo, os.environ.copy()
    ) == operations[1:]
    status = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-status", "--no-renames"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert set(status.splitlines()) == {
        "A\tcogs/after.py", "D\tcogs/before.py", "D\tcogs/obsolete.py"
    }
