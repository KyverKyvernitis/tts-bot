from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "scripts" / "migrate-dashboard-layout.sh"
UPDATER = caminho_fonte_core()
FRONT_BRIDGE = ROOT / "activity" / "sinuca" / "scripts" / "dashboard-bridge-build.mjs"
BACK_BRIDGE = ROOT / "activity" / "sinuca-server" / "scripts" / "dashboard-bridge-build.mjs"


def _package(path: Path, name: str, *, bridge: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"name": name}
    if bridge:
        data["scripts"] = {"build": "node scripts/dashboard-bridge-build.mjs"}
        scripts = path / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "dashboard-bridge-build.mjs").write_text("// bridge\n", encoding="utf-8")
    (path / "package.json").write_text(json.dumps(data), encoding="utf-8")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["REPO_DIR"] = str(repo)
    return subprocess.run(["bash", str(MIGRATION), *args], env=env, text=True, capture_output=True, check=False)


def _copy_bridge(repo: Path, kind: str) -> tuple[Path, Path]:
    if kind == "frontend":
        legacy = repo / "activity/sinuca"
        target = repo / "dashboard/frontend"
        source = FRONT_BRIDGE
    else:
        legacy = repo / "activity/sinuca-server"
        target = repo / "dashboard/backend"
        source = BACK_BRIDGE
    if not source.is_file():
        pytest.skip("bridge transitório removido após a finalização do layout")
    (legacy / "scripts").mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, legacy / "scripts/dashboard-bridge-build.mjs")
    (legacy / "package.json").write_text(
        json.dumps({"scripts": {"build": "node scripts/dashboard-bridge-build.mjs"}}), encoding="utf-8"
    )
    (target / "package.json").write_text(json.dumps({"name": f"canonical-{kind}"}), encoding="utf-8")
    return legacy, target


def _hydrate_only(script: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DASHBOARD_BRIDGE_HYDRATE_ONLY"] = "1"
    return subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, check=False)


def test_layout_migration_removes_only_known_bridges_and_preserves_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    _package(repo / "activity/sinuca", "osaka-dashboard-web", bridge=True)
    _package(repo / "activity/sinuca-server", "osaka-dashboard-server", bridge=True)
    (repo / "activity/sinuca-server/.env").write_text("SECRET=value\n", encoding="utf-8")

    check = _run(repo, "--check")
    assert check.returncode == 3

    result = _run(repo, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    assert not (repo / "activity").exists()
    assert (repo / "dashboard/backend/.env").read_text(encoding="utf-8") == "SECRET=value\n"
    assert _run(repo, "--apply").returncode == 0


def test_layout_migration_refuses_unknown_activity_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    _package(repo / "activity/sinuca", "unexpected-project")
    _package(repo / "activity/sinuca-server", "osaka-dashboard-server", bridge=True)

    result = _run(repo, "--apply")
    assert result.returncode == 1
    assert "não é uma ponte reconhecida" in result.stderr
    assert (repo / "activity/sinuca").exists()


def test_layout_migration_refuses_conflicting_local_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    _package(repo / "activity/sinuca", "osaka-dashboard-web", bridge=True)
    _package(repo / "activity/sinuca-server", "osaka-dashboard-server", bridge=True)
    (repo / "activity/sinuca-server/.env").write_text("SECRET=old\n", encoding="utf-8")
    (repo / "dashboard/backend/.env").write_text("SECRET=new\n", encoding="utf-8")

    result = _run(repo, "--apply")
    assert result.returncode != 0
    assert "conflito em configuração local" in result.stderr
    assert (repo / "activity/sinuca-server/.env").exists()


def test_frontend_bridge_hydrates_only_missing_files_and_preserves_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    legacy, target = _copy_bridge(repo, "frontend")
    (legacy / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (legacy / "src").mkdir()
    (legacy / "src/unchanged.ts").write_text("old-source\n", encoding="utf-8")
    (legacy / ".env").write_text("TOKEN=local\n", encoding="utf-8")
    (target / "src").mkdir()
    (target / "src/unchanged.ts").write_text("candidate-wins\n", encoding="utf-8")

    result = _hydrate_only(legacy / "scripts/dashboard-bridge-build.mjs")
    assert result.returncode == 0, result.stderr or result.stdout
    assert (target / "package-lock.json").read_text(encoding="utf-8") == '{"lockfileVersion":3}\n'
    assert (target / "src/unchanged.ts").read_text(encoding="utf-8") == "candidate-wins\n"
    assert (target / ".env").read_text(encoding="utf-8") == "TOKEN=local\n"
    assert not (target / "scripts/dashboard-bridge-build.mjs").exists()


def test_backend_bridge_refuses_conflicting_env_before_npm(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    legacy, target = _copy_bridge(repo, "backend")
    (legacy / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (legacy / ".env").write_text("TOKEN=old\n", encoding="utf-8")
    (target / ".env").write_text("TOKEN=new\n", encoding="utf-8")

    result = _hydrate_only(legacy / "scripts/dashboard-bridge-build.mjs")
    assert result.returncode != 0
    assert "conflito em configuração local" in result.stderr
    assert (target / ".env").read_text(encoding="utf-8") == "TOKEN=new\n"


def test_updater_uses_canonical_dashboard_paths_and_tests_before_build() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert 'FRONT_DIR="$REPO_DIR/dashboard/frontend"' in source
    assert 'BACK_DIR="$REPO_DIR/dashboard/backend"' in source
    assert "^(dashboard/frontend|activity/sinuca)/" in source
    assert "^(dashboard/backend|activity/sinuca-server)/" in source
    assert source.index('STAGE="testes do frontend"') < source.index('STAGE="build do frontend"')
    assert source.index('STAGE="testes do backend"') < source.index('STAGE="build do backend"')


def test_layout_check_is_noop_after_activity_bridge_is_gone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")

    result = _run(repo, "--check")
    assert result.returncode == 0
    assert "já está finalizado" in result.stdout


def test_layout_migration_requires_both_canonical_projects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "activity/sinuca", "osaka-dashboard-web", bridge=True)

    result = _run(repo, "--apply")
    assert result.returncode == 1
    assert "layout canônico incompleto" in result.stderr
    assert (repo / "activity/sinuca").exists()


def test_layout_migration_never_deletes_unrelated_activity_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    _package(repo / "activity/sinuca", "osaka-dashboard-web", bridge=True)
    _package(repo / "activity/sinuca-server", "osaka-dashboard-server", bridge=True)
    marker = repo / "activity/README-preservar.txt"
    marker.write_text("conteúdo não pertencente ao dashboard\n", encoding="utf-8")

    result = _run(repo, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    assert marker.read_text(encoding="utf-8") == "conteúdo não pertencente ao dashboard\n"
    assert not (repo / "activity/sinuca").exists()
    assert not (repo / "activity/sinuca-server").exists()


def test_layout_migration_moves_site_tests_to_dedicated_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")

    pairs = [
        ("test_activity_path_migration.py", "legacy activity test\n"),
        ("test_dashboard_architecture.py", "legacy architecture test\n"),
        ("test_dashboard_layout_migration.py", "legacy layout test\n"),
    ]
    for name, legacy_content in pairs:
        legacy = repo / "tests" / name
        canonical = repo / "tests" / "site" / name
        canonical.parent.mkdir(parents=True, exist_ok=True)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(legacy_content, encoding="utf-8")
        canonical.write_text(f"canonical {name}\n", encoding="utf-8")

    check = _run(repo, "--check")
    assert check.returncode == 3

    result = _run(repo, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    for name, _ in pairs:
        assert not (repo / "tests" / name).exists()
        assert (repo / "tests" / "site" / name).is_file()
    assert _run(repo, "--check").returncode == 0


def test_layout_migration_keeps_legacy_test_when_canonical_copy_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    legacy = repo / "tests" / "test_activity_path_migration.py"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("keep me\n", encoding="utf-8")

    result = _run(repo, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    assert legacy.read_text(encoding="utf-8") == "keep me\n"


def test_layout_migration_stages_site_test_move_with_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _package(repo / "dashboard/frontend", "osaka-dashboard-web")
    _package(repo / "dashboard/backend", "osaka-dashboard-server")
    legacy = repo / "tests" / "test_activity_path_migration.py"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=repo,
        check=True,
    )

    canonical = repo / "tests" / "site" / "test_activity_path_migration.py"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("canonical\n", encoding="utf-8")

    result = _run(repo, "--apply", "--stage")
    assert result.returncode == 0, result.stderr or result.stdout
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "tests/test_activity_path_migration.py" in staged
    assert "tests/site/test_activity_path_migration.py" in staged
    assert not legacy.exists()
    assert canonical.is_file()
