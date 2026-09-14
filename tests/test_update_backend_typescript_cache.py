from __future__ import annotations

import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[1]
UPDATER = caminho_fonte_core()


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
    (project / "src").mkdir(parents=True)
    (project / "scripts").mkdir(parents=True)
    (project / "src" / "index.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (project / "scripts" / "copy-command-catalog.mjs").write_text("// fixture\n", encoding="utf-8")
    (project / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "tsconfig.json").write_text(
        '{"compilerOptions":{"outDir":"dist","rootDir":"src","strict":true},"include":["src"]}\n',
        encoding="utf-8",
    )
    tsc = project / "node_modules" / ".bin" / "tsc"
    tsc.write_text(
        """#!/bin/sh
set -eu
info=''
prev=''
for arg in \"$@\"; do
  if [ \"$prev\" = '--tsBuildInfoFile' ]; then info=\"$arg\"; fi
  prev=\"$arg\"
done
[ -n \"$info\" ]
if [ -s \"$info\" ] && [ -s dist/index.js ]; then echo HIT >> \"$TSC_MARKER\"; else echo MISS >> \"$TSC_MARKER\"; fi
mkdir -p \"$(dirname \"$info\")\" dist
printf '{\"program\":\"ok\"}\\n' > \"$info\"
printf 'export const built = true;\\n' > dist/index.js
""",
        encoding="utf-8",
    )
    tsc.chmod(0o755)


def _harness(functions: Path, project: Path, cache: Path, marker: Path, extra: str) -> str:
    return f'''
source "{functions}"
TYPESCRIPT_CACHE_ROOT="{cache}"
TYPESCRIPT_CACHE_RETENTION=4
TYPESCRIPT_CACHE_HITS=0
TYPESCRIPT_CACHE_MISSES=0
LAST_BACKEND_TYPESCRIPT_CACHE_HIT=0
CHANGED_STATUS_RAW=''
LOG_TAG=test
export TSC_MARKER="{marker}"
node_dependency_cache_key() {{ printf '%064d\n' 0 | tr 0 c; }}
logger() {{ :; }}
chown() {{ :; }}
node() {{ :; }}
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
{extra}
'''


def test_backend_typescript_cache_seeds_dist_and_buildinfo_on_second_run(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "backend"
    cache = tmp_path / "typescript-cache"
    marker = tmp_path / "runs.txt"
    _fixture(project)

    result = _run(_harness(functions, project, cache, marker, f'''
run_backend_incremental_build "{project}"
first="$LAST_BACKEND_TYPESCRIPT_CACHE_HIT"
rm -rf "{project}/dist" "{project}/.update-tscache-backend"
run_backend_incremental_build "{project}"
second="$LAST_BACKEND_TYPESCRIPT_CACHE_HIT"
printf 'FIRST=%s SECOND=%s HITS=%s MISSES=%s\n' "$first" "$second" "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
'''))
    assert "FIRST=0 SECOND=1 HITS=1 MISSES=1" in result.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "HIT"]


def test_backend_typescript_cache_key_changes_with_tsconfig(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "backend"
    _fixture(project)

    result = _run(_harness(functions, project, tmp_path / "cache", tmp_path / "marker", f'''
key1="$(backend_typescript_cache_key "{project}")"
printf '{{"compilerOptions":{{"strict":false}}}}\n' > "{project}/tsconfig.json"
key2="$(backend_typescript_cache_key "{project}")"
[[ "$key1" != "$key2" ]]
printf 'DIFFER=1\n'
'''))
    assert result.stdout.strip() == "DIFFER=1"


def test_backend_source_deletion_forces_clean_build(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "backend"
    cache = tmp_path / "typescript-cache"
    marker = tmp_path / "runs.txt"
    _fixture(project)

    result = _run(_harness(functions, project, cache, marker, f'''
CHANGED_STATUS_RAW=''
run_backend_incremental_build "{project}"
CHANGED_STATUS_RAW=$'D\tdashboard/backend/src/old.ts'
rm -rf "{project}/dist" "{project}/.update-tscache-backend"
run_backend_incremental_build "{project}"
printf 'SECOND=%s HITS=%s MISSES=%s\n' "$LAST_BACKEND_TYPESCRIPT_CACHE_HIT" "$TYPESCRIPT_CACHE_HITS" "$TYPESCRIPT_CACHE_MISSES"
'''))
    assert "SECOND=0 HITS=0 MISSES=2" in result.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["MISS", "MISS"]


def test_backend_cache_manifest_detects_dist_tampering(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "typescript-functions.sh"
    functions.write_text(_typescript_functions(source), encoding="utf-8")
    project = tmp_path / "backend"
    cache = tmp_path / "typescript-cache"
    marker = tmp_path / "runs.txt"
    _fixture(project)

    result = _run(_harness(functions, project, cache, marker, f'''
run_backend_incremental_build "{project}"
key="$(backend_typescript_cache_key "{project}")"
entry="$(backend_typescript_cache_entry "$key")"
chmod -R u+w "$entry"
printf 'tampered\n' > "$entry/dist/index.js"
if verify_backend_typescript_cache "$entry" "$key"; then exit 9; fi
printf 'TAMPER_REJECTED=1\n'
'''))
    assert "TAMPER_REJECTED=1" in result.stdout


def test_backend_incremental_build_emits_and_runs_postbuild() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _typescript_functions(source)
    assert "backend-emit-v1" in block
    assert "--incremental --tsBuildInfoFile .update-tscache-backend/tsconfig.tsbuildinfo" in block
    assert "node scripts/copy-command-catalog.mjs" in block
