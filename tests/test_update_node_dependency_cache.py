from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _cache_functions(source: str) -> str:
    start = source.index("node_dependency_cache_key() {")
    end = source.index("\nprepare_local_candidate_runtime_artifacts_in_worktree() {", start)
    return source[start:end]


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_fixture(project: Path) -> None:
    project.mkdir(parents=True)
    (project / "package.json").write_text(
        '{"name":"fixture","version":"1.0.0","private":true}\n', encoding="utf-8"
    )
    (project / "package-lock.json").write_text(
        '{"name":"fixture","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{}}\n',
        encoding="utf-8",
    )


def test_node_dependency_cache_reuses_layer_without_second_install(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    project = tmp_path / "project"
    cache = tmp_path / "cache"
    _write_fixture(project)

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
NODE_DEPENDENCY_CACHE_RETENTION=4
NPM_INSTALL_FLAGS="--prefer-offline --no-audit --no-fund --progress=false"
NODE_DEP_CACHE_HITS=0
NODE_DEP_CACHE_MISSES=0
LAST_NODE_DEP_LAYER_PATH=''
LAST_NODE_DEP_LAYER_KEY=''
LAST_NODE_DEP_CACHE_HIT=0
LOG_TAG=test
INSTALL_COUNT=0
PROJECT="{project}"
logger() {{ :; }}
chown() {{ :; }}
install() {{
  local -a args=()
  while (($#)); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  command install "${{args[@]}}"
}}
sudo() {{
  if [[ "${{1:-}}" == "-u" ]]; then shift 2; fi
  if [[ "${{1:-}}" == "-H" ]]; then shift; fi
  "$@"
}}
zip_progress_run_as_ubuntu() {{
  INSTALL_COUNT=$((INSTALL_COUNT + 1))
  mkdir -p "$PROJECT/node_modules/pkg"
  printf 'installed-%s\n' "$INSTALL_COUNT" > "$PROJECT/node_modules/pkg/index.js"
}}
prepare_node_dependency_layer "$PROJECT" frontend dev title detail
first_layer="$LAST_NODE_DEP_LAYER_PATH"
[[ -L "$PROJECT/node_modules" ]]
[[ -f "$first_layer/node_modules/pkg/index.js" ]]
prepare_node_dependency_layer "$PROJECT" frontend dev title detail
[[ "$LAST_NODE_DEP_CACHE_HIT" == 1 ]]
[[ "$LAST_NODE_DEP_LAYER_PATH" == "$first_layer" ]]
[[ "$INSTALL_COUNT" == 1 ]]
printf 'INSTALLS=%s HITS=%s MISSES=%s\n' "$INSTALL_COUNT" "$NODE_DEP_CACHE_HITS" "$NODE_DEP_CACHE_MISSES"
'''
    result = _run_bash(harness)
    assert "INSTALLS=1 HITS=1 MISSES=1" in result.stdout


def test_node_dependency_cache_key_changes_with_lockfile(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    project = tmp_path / "project"
    cache = tmp_path / "cache"
    _write_fixture(project)

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
NODE_DEPENDENCY_CACHE_RETENTION=4
NPM_INSTALL_FLAGS="--prefer-offline --no-audit --no-fund --progress=false"
NODE_DEP_CACHE_HITS=0
NODE_DEP_CACHE_MISSES=0
LAST_NODE_DEP_LAYER_PATH=''
LAST_NODE_DEP_LAYER_KEY=''
LAST_NODE_DEP_CACHE_HIT=0
LOG_TAG=test
INSTALL_COUNT=0
PROJECT="{project}"
logger() {{ :; }}
chown() {{ :; }}
install() {{ local -a a=(); while (($#)); do case "$1" in -o|-g) shift 2;; *) a+=("$1"); shift;; esac; done; command install "${{a[@]}}"; }}
sudo() {{ if [[ "${{1:-}}" == -u ]]; then shift 2; fi; if [[ "${{1:-}}" == -H ]]; then shift; fi; "$@"; }}
zip_progress_run_as_ubuntu() {{ INSTALL_COUNT=$((INSTALL_COUNT + 1)); mkdir -p "$PROJECT/node_modules/pkg"; echo "$INSTALL_COUNT" > "$PROJECT/node_modules/pkg/index.js"; }}
prepare_node_dependency_layer "$PROJECT" frontend dev x y
key1="$LAST_NODE_DEP_LAYER_KEY"
printf '\n' >> "$PROJECT/package-lock.json"
prepare_node_dependency_layer "$PROJECT" frontend dev x y
key2="$LAST_NODE_DEP_LAYER_KEY"
[[ "$key1" != "$key2" ]]
[[ "$INSTALL_COUNT" == 2 ]]
printf 'INSTALLS=%s KEYS_DIFFER=1\n' "$INSTALL_COUNT"
'''
    result = _run_bash(harness)
    assert "INSTALLS=2 KEYS_DIFFER=1" in result.stdout


def test_backend_has_separate_dev_and_prod_dependency_layers() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("prepare_local_candidate_runtime_artifacts_in_worktree() {")
    end = source.index("\npromote_local_candidate_worktree_commit() {", start)
    block = source[start:end]

    assert 'prepare_node_dependency_layer "$back_dir" backend dev' in block
    assert 'prepare_node_dependency_layer "$back_dir" backend prod' in block
    assert 'backend_runtime_modules="$LAST_NODE_DEP_LAYER_PATH/node_modules"' in block
    assert 'npm run build && npm prune --omit=dev' not in block


def test_npm_cache_is_not_cleaned_and_network_extras_are_disabled() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert "npm cache clean --force" not in source
    assert 'NPM_INSTALL_FLAGS="--prefer-offline --no-audit --no-fund --progress=false"' in source


def test_dependency_layer_preserves_install_failure_exit_code(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    project = tmp_path / "project"
    cache = tmp_path / "cache"
    _write_fixture(project)

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
NODE_DEPENDENCY_CACHE_RETENTION=4
NPM_INSTALL_FLAGS="--prefer-offline --no-audit --no-fund --progress=false"
NODE_DEP_CACHE_HITS=0
NODE_DEP_CACHE_MISSES=0
LAST_NODE_DEP_LAYER_PATH=''
LAST_NODE_DEP_LAYER_KEY=''
LAST_NODE_DEP_CACHE_HIT=0
LOG_TAG=test
logger() {{ :; }}
chown() {{ :; }}
install() {{ local -a a=(); while (($#)); do case "$1" in -o|-g) shift 2;; *) a+=("$1"); shift;; esac; done; command install "${{a[@]}}"; }}
sudo() {{ if [[ "${{1:-}}" == -u ]]; then shift 2; fi; if [[ "${{1:-}}" == -H ]]; then shift; fi; "$@"; }}
zip_progress_run_as_ubuntu() {{ return 23; }}
set +e
prepare_node_dependency_layer "{project}" frontend dev x y
rc=$?
set -e
printf 'RC=%s\n' "$rc"
[[ "$rc" == 23 ]]
'''
    result = _run_bash(harness)
    assert "RC=23" in result.stdout


def test_dependency_cache_retention_prunes_old_layers(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "cache-functions.sh"
    functions.write_text(_cache_functions(source), encoding="utf-8")
    cache = tmp_path / "cache"
    group = cache / "frontend" / "dev"
    for idx in range(5):
        layer = group / f"{'a' * 63}{idx}"
        (layer / "node_modules").mkdir(parents=True)
        (layer / "layer.json").write_text("{}", encoding="utf-8")
        subprocess.run(["touch", "-d", f"2026-01-0{idx + 1} 00:00:00", str(layer)], check=True)

    harness = f'''
source "{functions}"
NODE_DEPENDENCY_CACHE_ROOT="{cache}"
NODE_DEPENDENCY_CACHE_RETENTION=2
prune_node_dependency_layers
find "{group}" -mindepth 1 -maxdepth 1 -type d | wc -l
'''
    result = _run_bash(harness)
    assert result.stdout.strip() == "2"
