from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts" / "migrate-activity-directory.sh"


def _run(repo: Path, *args: str, backup: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"REPO_DIR": str(repo), "ACTIVITY_PATH_BACKUP": str(backup)})
    return subprocess.run(
        ["bash", str(MIGRATION), *args],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_activity_directory_migration_preserves_new_files_and_local_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "activity "
    target = repo / "activity"
    (legacy / "sinuca" / "src").mkdir(parents=True)
    (legacy / "sinuca-server").mkdir(parents=True)
    (target / "sinuca" / "src").mkdir(parents=True)

    (legacy / "sinuca" / "src" / "kept.ts").write_text("legacy-only\n", encoding="utf-8")
    (legacy / "sinuca" / "src" / "updated.ts").write_text("old\n", encoding="utf-8")
    (target / "sinuca" / "src" / "updated.ts").write_text("new\n", encoding="utf-8")
    (legacy / "sinuca-server" / ".env").write_text("SECRET=preserve\n", encoding="utf-8")

    backup = tmp_path / "activity-backup.tar.gz"
    result = _run(repo, "--apply", backup=backup)

    assert result.returncode == 0, result.stderr or result.stdout
    assert not legacy.exists()
    assert (target / "sinuca" / "src" / "kept.ts").read_text(encoding="utf-8") == "legacy-only\n"
    assert (target / "sinuca" / "src" / "updated.ts").read_text(encoding="utf-8") == "new\n"
    assert (target / "sinuca-server" / ".env").read_text(encoding="utf-8") == "SECRET=preserve\n"
    assert backup.is_file()

    second = _run(repo, "--apply", backup=tmp_path / "unused.tar.gz")
    assert second.returncode == 0
    assert "Nada a migrar" in second.stdout


def test_activity_directory_migration_check_reports_pending_move(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "activity ").mkdir(parents=True)
    result = _run(repo, "--check", backup=tmp_path / "backup.tar.gz")
    assert result.returncode == 3
    assert "Migração necessária" in result.stdout



def test_activity_directory_migration_discards_generated_node_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "activity "
    target = repo / "activity"

    (legacy / "sinuca-server" / "node_modules" / ".bin").mkdir(parents=True)
    (legacy / "sinuca-server" / "node_modules" / "mime").mkdir(parents=True)
    (legacy / "sinuca-server" / "dist").mkdir(parents=True)
    (legacy / "sinuca-server" / "src").mkdir(parents=True)
    (target / "sinuca-server" / "src").mkdir(parents=True)

    (legacy / "sinuca-server" / "node_modules" / "mime" / "cli.js").write_text(
        "generated\n", encoding="utf-8"
    )
    (legacy / "sinuca-server" / "node_modules" / ".bin" / "mime").symlink_to(
        "../mime/cli.js"
    )
    (legacy / "sinuca-server" / "dist" / "index.js").write_text(
        "generated\n", encoding="utf-8"
    )
    (legacy / "sinuca-server" / "src" / "legacy-only.ts").write_text(
        "keep\n", encoding="utf-8"
    )

    backup = tmp_path / "activity-backup.tar.gz"
    result = _run(repo, "--apply", backup=backup)

    assert result.returncode == 0, result.stderr or result.stdout
    assert not legacy.exists()
    assert (target / "sinuca-server" / "src" / "legacy-only.ts").read_text(
        encoding="utf-8"
    ) == "keep\n"
    assert not (target / "sinuca-server" / "node_modules").exists()
    assert not (target / "sinuca-server" / "dist").exists()
    assert backup.is_file()
