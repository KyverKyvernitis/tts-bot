from __future__ import annotations

import os
import errno
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from updater.testes.test_cache_typescript import _fixture as frontend_fixture
from updater.testes.test_cache_typescript_backend import (
    UPDATER,
    _fixture as backend_fixture,
    _harness,
    _typescript_functions,
)


def _functions(directory: Path) -> Path:
    path = directory / "functions.sh"
    path.write_text(_typescript_functions(UPDATER.read_text(encoding="utf-8")), encoding="utf-8")
    return path


def _run_name(kind: str) -> str:
    return "run_frontend_incremental_typecheck" if kind == "frontend" else "run_backend_incremental_build"


@pytest.mark.parametrize("kind", ["frontend", "backend"])
@pytest.mark.parametrize("legacy", [False, True], ids=["new-cache", "legacy-root-0500"])
def test_cache_can_be_reused_by_a_real_unprivileged_build_user(kind: str, legacy: bool) -> None:
    if os.geteuid() != 0 or not shutil.which("runuser"):
        pytest.skip("Validação de dois usuários requer root e runuser; não altera contas do sistema.")
    try:
        user = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("Usuário não privilegiado nobody indisponível.")
    with tempfile.TemporaryDirectory(prefix="updater-ts-permissions-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        project = directory / kind
        (frontend_fixture if kind == "frontend" else backend_fixture)(project)
        try:
            for path in [project, *project.rglob("*")]:
                os.chown(path, user.pw_uid, user.pw_gid)
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.EPERM):
                pytest.skip("Namespace do CI não permite mapear um segundo UID/GID.")
            raise
        functions = _functions(directory)
        cache = directory / "cache"
        marker = project / "runs.txt"
        mode = "chmod 0500 \"$entry\"" if legacy else ":"
        script = _harness(functions, project, cache, marker, f'''
# Use identidades reais sem depender da existência da conta ubuntu no CI.
install() {{
  local -a args=()
  for arg in "$@"; do
    if [[ "$arg" == ubuntu ]]; then
      [[ "${{args[-1]}}" == -g ]] && arg="{user.pw_gid}" || arg="{user.pw_uid}"
    fi
    args+=("$arg")
  done
  command install "${{args[@]}}"
}}
chown() {{
  local -a args=()
  for arg in "$@"; do
    [[ "$arg" == ubuntu:ubuntu ]] && arg="{user.pw_uid}:{user.pw_gid}"
    args+=("$arg")
  done
  command chown "${{args[@]}}"
}}
sudo() {{
  [[ "${{1:-}}" == -u ]] && shift 2
  [[ "${{1:-}}" == -H ]] && shift
  command runuser -u nobody -- "$@"
}}
zip_progress_run_as_ubuntu() {{ command runuser -u nobody -- bash -c "$3"; }}
{_run_name(kind)} "{project}"
key="$({kind}_typescript_cache_key "{project}")"
entry="$({kind}_typescript_cache_entry "$key")"
{mode}
{_run_name(kind)} "{project}"
sudo -u ubuntu -H test -r "$entry/tsconfig.tsbuildinfo"
if sudo -u ubuntu -H test -w "$entry/tsconfig.tsbuildinfo"; then exit 8; fi
if sudo -u ubuntu -H test -w "$entry"; then exit 9; fi
printf 'HITS=%s MISSES=%s\n' "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
''')
        result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", script], text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "HITS=1 MISSES=1" in result.stdout
        assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "HIT"]


@pytest.mark.parametrize("kind", ["frontend", "backend"])
@pytest.mark.parametrize("legacy", [False, True], ids=["new-cache", "legacy-root-0500"])
def test_shared_cache_modes_allow_reading_without_write(tmp_path: Path, kind: str, legacy: bool) -> None:
    project = tmp_path / kind
    (frontend_fixture if kind == "frontend" else backend_fixture)(project)
    functions = _functions(tmp_path)
    cache = tmp_path / "cache"
    marker = project / "runs.txt"
    mode = 'chmod 0500 "$entry"' if legacy else ":"
    script = _harness(functions, project, cache, marker, f'''
{_run_name(kind)} "{project}"
key="$({kind}_typescript_cache_key "{project}")"
entry="$({kind}_typescript_cache_entry "$key")"
{mode}
{_run_name(kind)} "{project}"
printf 'HITS=%s MISSES=%s\n' "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
''')
    result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HITS=1 MISSES=1" in result.stdout
    entries = list((cache / kind).iterdir())
    assert len(entries) == 1
    for path in [entries[0], *entries[0].rglob("*")]:
        mode = path.stat().st_mode & 0o777
        assert mode & 0o222 == 0, (path, oct(mode))
        assert mode & (0o055 if path.is_dir() else 0o044) == (0o055 if path.is_dir() else 0o044), (path, oct(mode))
    assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "HIT"]


@pytest.mark.parametrize("kind, failure", [("frontend", "install"), ("backend", "install"), ("backend", "cp")])
def test_cache_copy_failure_discards_partial_seed_and_rebuilds(tmp_path: Path, kind: str, failure: str) -> None:
    project = tmp_path / kind
    (frontend_fixture if kind == "frontend" else backend_fixture)(project)
    functions = _functions(tmp_path)
    marker = project / "runs.txt"
    script = _harness(functions, project, tmp_path / "cache", marker, f'''
{_run_name(kind)} "{project}"
key="$({kind}_typescript_cache_key "{project}")"
entry="$({kind}_typescript_cache_entry "$key")"
sudo() {{
  [[ "${{1:-}}" == -u ]] && shift 2
  [[ "${{1:-}}" == -H ]] && shift
  if [[ "$1" == "{failure}" && "$*" == *"$entry/"* ]]; then
    if [[ "$1" == install ]]; then
      printf 'partial' > "${{!#}}"
    else
      mkdir -p "${{!#}}"
      printf 'partial' > "${{!#}}/index.js"
      printf 'stale' > "${{!#}}/orphan.js"
    fi
    return 1
  fi
  "$@"
}}
{_run_name(kind)} "{project}"
[[ ! -e "{project}/dist/orphan.js" ]]
printf 'HITS=%s MISSES=%s\n' "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
''')
    result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HITS=0 MISSES=2" in result.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "MISS"]
