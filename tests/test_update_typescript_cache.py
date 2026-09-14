from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "updater" / "core" / "atualizar.sh"


def _typescript_functions(source: str) -> str:
    start = source.index("frontend_typescript_cache_key() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    return source[start:end]


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture(project: Path) -> None:
    (project / "node_modules" / ".bin").mkdir(parents=True)
    (project / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    tsc = project / "node_modules" / ".bin" / "tsc"
    tsc.write_text(
        """#!/bin/sh
set -eu
info=''
prev=''
for arg in "$@"; do
  if [ "$prev" = '--tsBuildInfoFile' ]; then info="$arg"; fi
  prev="$arg"
done
[ -n "$info" ]
if [ -s "$info" ]; then echo HIT >> "$TSC_MARKER"; else echo MISS >> "$TSC_MARKER"; fi
mkdir -p "$(dirname "$info")"
printf '{"program":"ok"}\n' > "$info"
""",
        encoding="utf-8",
    )
    tsc.chmod(0o755)


def test_frontend_typescript_cache_seeds_second_incremental_run(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "frontend"
    cache = tmp_path / "typescript-cache"
    marker = tmp_path / "runs.txt"
    _fixture(project)

    harness = f'''
source "{functions}"
TYPESCRIPT_CACHE_ROOT="{cache}"
TYPESCRIPT_CACHE_RETENTION=4
TYPESCRIPT_CACHE_HITS=0
TYPESCRIPT_CACHE_MISSES=0
LAST_TYPESCRIPT_CACHE_HIT=0
LOG_TAG=test
export TSC_MARKER="{marker}"
node_dependency_cache_key() {{ printf '%064d\n' 0 | tr 0 a; }}
logger() {{ :; }}
chown() {{ :; }}
install() {{
  local -a a=()
  while (($#)); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) a+=("$1"); shift ;;
    esac
  done
  command install "${{a[@]}}"
}}
sudo() {{
  if [[ "${{1:-}}" == -u ]]; then shift 2; fi
  [[ "${{1:-}}" == -H ]] && shift
  "$@"
}}
zip_progress_run_as_ubuntu() {{ eval "$3"; }}
run_frontend_incremental_typecheck "{project}"
first="$LAST_TYPESCRIPT_CACHE_HIT"
run_frontend_incremental_typecheck "{project}"
second="$LAST_TYPESCRIPT_CACHE_HIT"
printf 'FIRST=%s SECOND=%s HITS=%s MISSES=%s\n' "$first" "$second" "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
'''
    result = _run(harness)
    assert "FIRST=0 SECOND=1 HITS=1 MISSES=1" in result.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "HIT"]


def test_frontend_typescript_cache_key_changes_with_tsconfig(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "frontend"
    _fixture(project)

    harness = f'''
source "{functions}"
node_dependency_cache_key() {{ printf '%064d\n' 0 | tr 0 b; }}
key1="$(frontend_typescript_cache_key "{project}")"
printf '\n{{"compilerOptions":{{"strict":false}}}}\n' > "{project}/tsconfig.json"
key2="$(frontend_typescript_cache_key "{project}")"
[[ "$key1" != "$key2" ]]
printf 'DIFFER=1\n'
'''
    result = _run(harness)
    assert result.stdout.strip() == "DIFFER=1"


def test_frontend_typecheck_uses_incremental_buildinfo_and_no_emit() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _typescript_functions(source)
    assert "--incremental" in block
    assert "--tsBuildInfoFile .update-tscache/tsconfig.tsbuildinfo" in block
    assert "--noEmit" in block
    assert "cache TypeScript HIT" in block


def test_typescript_cache_retention_prunes_old_entries(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    group = tmp_path / "cache" / "frontend"
    for idx in range(5):
        entry = group / (str(idx) * 64)
        entry.mkdir(parents=True)
        (entry / "cache.json").write_text("{}", encoding="utf-8")
        subprocess.run(["touch", "-d", f"2026-01-0{idx + 1} 00:00:00", str(entry)], check=True)

    result = _run(
        f'''source "{functions}"
TYPESCRIPT_CACHE_ROOT="{tmp_path / 'cache'}"
TYPESCRIPT_CACHE_RETENTION=2
prune_typescript_cache
find "{group}" -mindepth 1 -maxdepth 1 -type d | wc -l
'''
    )
    assert result.stdout.strip() == "2"
