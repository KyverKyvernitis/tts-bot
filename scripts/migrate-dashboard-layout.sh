#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
MODE="check"
STAGE_GIT=0

for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    --check) MODE="check" ;;
    --stage) STAGE_GIT=1 ;;
    *) echo "argumento desconhecido: $arg" >&2; exit 2 ;;
  esac
done

FRONT="$REPO_DIR/dashboard/frontend"
BACK="$REPO_DIR/dashboard/backend"
LEGACY_FRONT="$REPO_DIR/activity/sinuca"
LEGACY_BACK="$REPO_DIR/activity/sinuca-server"
SITE_TEST_DIR="$REPO_DIR/tests/site"
LEGACY_SITE_TESTS=(
  "$REPO_DIR/tests/test_activity_path_migration.py"
  "$REPO_DIR/tests/test_dashboard_architecture.py"
  "$REPO_DIR/tests/test_dashboard_layout_migration.py"
)
CANONICAL_SITE_TESTS=(
  "$SITE_TEST_DIR/test_activity_path_migration.py"
  "$SITE_TEST_DIR/test_dashboard_architecture.py"
  "$SITE_TEST_DIR/test_dashboard_layout_migration.py"
)

if [[ ! -f "$FRONT/package.json" || ! -f "$BACK/package.json" ]]; then
  echo "layout canônico incompleto em $REPO_DIR/dashboard" >&2
  exit 1
fi

is_bridge() {
  local path="$1"
  [[ -f "$path/package.json" && -f "$path/scripts/dashboard-bridge-build.mjs" ]] || return 1
  python3 - "$path/package.json" <<'PY'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if data.get("scripts", {}).get("build") == "node scripts/dashboard-bridge-build.mjs" else 1)
PY
}

pending=0
for path in "$LEGACY_FRONT" "$LEGACY_BACK"; do
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ -L "$path" ]] || is_bridge "$path"; then
      pending=1
    else
      echo "diretório activity não é uma ponte reconhecida; migração recusada: $path" >&2
      exit 1
    fi
  fi
done

for i in "${!LEGACY_SITE_TESTS[@]}"; do
  legacy_test="${LEGACY_SITE_TESTS[$i]}"
  canonical_test="${CANONICAL_SITE_TESTS[$i]}"
  if [[ -f "$legacy_test" && -f "$canonical_test" ]]; then
    pending=1
  fi
done

if (( pending == 0 )); then
  echo "Layout dashboard já está finalizado; testes do site também. Nada a migrar."
  exit 0
fi
if [[ "$MODE" != "apply" ]]; then
  echo "Finalização necessária: remover ponte activity e manter dashboard como fonte canônica."
  exit 3
fi

REPO_DIR="$REPO_DIR" python3 - <<'PY'
from __future__ import annotations
import filecmp, json, os, pathlib, shutil

repo = pathlib.Path(os.environ["REPO_DIR"]).resolve()
pairs = [
    (repo / "activity" / "sinuca", repo / "dashboard" / "frontend"),
    (repo / "activity" / "sinuca-server", repo / "dashboard" / "backend"),
]
templates = {".env.example", ".env.sample", ".env.template"}

def sensitive(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name not in templates)

for legacy, target in pairs:
    if not legacy.exists() and not legacy.is_symlink():
        continue
    if legacy.is_symlink():
        legacy.unlink()
        continue
    data = json.loads((legacy / "package.json").read_text(encoding="utf-8"))
    if data.get("scripts", {}).get("build") != "node scripts/dashboard-bridge-build.mjs" or not (legacy / "scripts" / "dashboard-bridge-build.mjs").is_file():
        raise SystemExit(f"ponte não reconhecida: {legacy}")
    for item in legacy.iterdir():
        if not item.is_file() or not sensitive(item.name):
            continue
        destination = target / item.name
        if destination.exists():
            if not destination.is_file() or not filecmp.cmp(item, destination, shallow=False):
                raise SystemExit(f"conflito em configuração local: {item.name}")
        else:
            shutil.copy2(item, destination)
    shutil.rmtree(legacy)

activity = repo / "activity"
try:
    activity.rmdir()
except OSError:
    pass

site_pairs = [
    (repo / "tests" / "test_activity_path_migration.py", repo / "tests" / "site" / "test_activity_path_migration.py"),
    (repo / "tests" / "test_dashboard_architecture.py", repo / "tests" / "site" / "test_dashboard_architecture.py"),
    (repo / "tests" / "test_dashboard_layout_migration.py", repo / "tests" / "site" / "test_dashboard_layout_migration.py"),
]
for legacy_test, canonical_test in site_pairs:
    if canonical_test.is_file() and legacy_test.is_file():
        legacy_test.unlink()
PY

if (( STAGE_GIT == 1 )); then
  [[ -d "$REPO_DIR/.git" ]] || { echo "--stage exige repositório Git" >&2; exit 1; }
  stage_paths=()
  for rel in     activity     dashboard     tests/site     tests/test_activity_path_migration.py     tests/test_dashboard_architecture.py     tests/test_dashboard_layout_migration.py
  do
    if [[ -e "$REPO_DIR/$rel" || -L "$REPO_DIR/$rel" ]] || git -C "$REPO_DIR" ls-files -- "$rel" | grep -q .; then
      stage_paths+=("$rel")
    fi
  done
  if (( ${#stage_paths[@]} > 0 )); then
    git -C "$REPO_DIR" add -A -- "${stage_paths[@]}"
  fi
fi

echo "Layout finalizado em: $REPO_DIR/dashboard (testes do site em tests/site)"
