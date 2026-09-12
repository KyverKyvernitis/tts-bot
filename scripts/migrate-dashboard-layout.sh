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
generated = {"node_modules", "dist", ".vite", ".cache", "coverage"}
bridge_control = {"package.json", "scripts/dashboard-bridge-build.mjs"}

def sensitive(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name not in templates)

def hydrate_missing(legacy: pathlib.Path, target: pathlib.Path) -> None:
    # O updater novo executa esta migração antes do build. Durante uma retomada
    # parcial (backend já canônico, frontend ainda legado), hidrate primeiro os
    # arquivos canônicos ausentes e só então remova a ponte. Nunca sobrescreva
    # arquivos enviados pelo candidato atual.
    for source in legacy.rglob("*"):
        rel = source.relative_to(legacy)
        if any(part in generated for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if rel_posix in bridge_control or sensitive(source.name):
            continue
        destination = target / rel
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # package-lock.json é parte do bootstrap reprodutível. Rollbacks do
        # updater podem deixar uma cópia canônica não rastreada de tentativas
        # anteriores; nunca preserve silenciosamente esse lock residual.
        if source.name == "package-lock.json":
            shutil.copy2(source, destination)
            continue
        if destination.exists():
            continue
        shutil.copy2(source, destination)

for legacy, target in pairs:
    if not legacy.exists() and not legacy.is_symlink():
        continue
    if legacy.is_symlink():
        legacy.unlink()
        continue
    data = json.loads((legacy / "package.json").read_text(encoding="utf-8"))
    if data.get("scripts", {}).get("build") != "node scripts/dashboard-bridge-build.mjs" or not (legacy / "scripts" / "dashboard-bridge-build.mjs").is_file():
        raise SystemExit(f"ponte não reconhecida: {legacy}")
    hydrate_missing(legacy, target)
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

# Tentativas abortadas podem deixar node_modules/.vite no frontend canônico.
# O updater executa npm como ubuntu, enquanto a aplicação/migração roda como
# root; remova artefatos gerados aqui para que npm ci não precise apagar uma
# árvore com ownership herdado de uma tentativa anterior.
for generated_name in ("node_modules", ".vite"):
    generated_path = repo / "dashboard" / "frontend" / generated_name
    if generated_path.is_symlink() or generated_path.is_file():
        generated_path.unlink(missing_ok=True)
    elif generated_path.is_dir():
        shutil.rmtree(generated_path)

site_pairs = [
    (repo / "tests" / "test_activity_path_migration.py", repo / "tests" / "site" / "test_activity_path_migration.py"),
    (repo / "tests" / "test_dashboard_architecture.py", repo / "tests" / "site" / "test_dashboard_architecture.py"),
    (repo / "tests" / "test_dashboard_layout_migration.py", repo / "tests" / "site" / "test_dashboard_layout_migration.py"),
]
for legacy_test, canonical_test in site_pairs:
    if canonical_test.is_file() and legacy_test.is_file():
        legacy_test.unlink()
PY

# Na VPS o npm sempre roda como ubuntu. Garanta que tanto o frontend hidratado
# quanto o cache npm possam ser escritos por esse usuário mesmo após rollbacks
# interrompidos executados pelo updater root. Isso é restrito ao checkout
# operacional/owner ubuntu para não alterar ownership de repositórios de teste.
repo_owner="$(stat -c '%U' "$REPO_DIR" 2>/dev/null || true)"
if [[ "$(id -u)" == "0" ]] && id -u ubuntu >/dev/null 2>&1 && \
   [[ "$REPO_DIR" == "/home/ubuntu/bot" || "$repo_owner" == "ubuntu" ]]; then
  chown -R ubuntu:ubuntu "$FRONT" 2>/dev/null || true
  chmod -R u+rwX "$FRONT" 2>/dev/null || true
  ubuntu_home="$(getent passwd ubuntu 2>/dev/null | cut -d: -f6)"
  [[ -n "$ubuntu_home" ]] || ubuntu_home="/home/ubuntu"
  mkdir -p "$ubuntu_home/.npm" 2>/dev/null || true
  chown -R ubuntu:ubuntu "$ubuntu_home/.npm" 2>/dev/null || true
  chmod -R u+rwX "$ubuntu_home/.npm" 2>/dev/null || true
fi

# Normalize o lock com a mesma versão de npm da VPS antes de o updater
# executar `npm ci`. `--package-lock-only` não cria node_modules e
# `--ignore-scripts` impede lifecycle scripts nesta etapa de bootstrap.
normalize_frontend_lock=0
if [[ -f "$FRONT/package.json" && -f "$FRONT/package-lock.json" ]]; then
  normalize_frontend_lock="$(python3 - "$FRONT/package.json" <<'LOCKCHECK'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print(0)
else:
    print(1 if (data.get("dependencies") or data.get("devDependencies")) else 0)
LOCKCHECK
)"
fi

if [[ "$normalize_frontend_lock" == "1" && "${DASHBOARD_SKIP_NPM_LOCK_NORMALIZE:-0}" != "1" ]]; then
  npm_lock_log="$(mktemp "${TMPDIR:-/tmp}/dashboard-npm-lock.XXXXXX")"
  npm_lock_rc=0
  npm_lock_cmd=(npm install --package-lock-only --ignore-scripts --include=dev --no-audit --no-fund)

  if [[ "$(id -u)" == "0" ]] && id -u ubuntu >/dev/null 2>&1 && \
     [[ "$REPO_DIR" == "/home/ubuntu/bot" || "$repo_owner" == "ubuntu" ]]; then
    ubuntu_home="$(getent passwd ubuntu 2>/dev/null | cut -d: -f6)"
    [[ -n "$ubuntu_home" ]] || ubuntu_home="/home/ubuntu"
    if (cd "$FRONT" && sudo -u ubuntu -H env \
        HOME="$ubuntu_home" \
        npm_config_cache="$ubuntu_home/.npm" \
        npm_config_engine_strict=false \
        "${npm_lock_cmd[@]}") >"$npm_lock_log" 2>&1; then
      npm_lock_rc=0
    else
      npm_lock_rc=$?
    fi
  else
    if (cd "$FRONT" && env npm_config_engine_strict=false "${npm_lock_cmd[@]}") >"$npm_lock_log" 2>&1; then
      npm_lock_rc=0
    else
      npm_lock_rc=$?
    fi
  fi

  if (( npm_lock_rc != 0 )); then
    echo "falha ao normalizar package-lock do frontend com o npm local (rc=$npm_lock_rc)" >&2
    cat "$npm_lock_log" >&2 || true
    rm -f "$npm_lock_log" 2>/dev/null || true
    exit "$npm_lock_rc"
  fi
  rm -f "$npm_lock_log" 2>/dev/null || true
fi

if (( STAGE_GIT == 1 )); then
  [[ -d "$REPO_DIR/.git" ]] || { echo "--stage exige repositório Git" >&2; exit 1; }

  # Bootstrap da migração: o updater que aplica este ZIP ainda é a cópia estável
  # do commit anterior. Na VPS ele roda como root e, em tentativas abortadas,
  # metadados de .git podem ter ficado com ownership/permissões heterogêneos.
  # Faça o único stage necessário no mesmo usuário privilegiado do updater,
  # liberando explicitamente apenas este checkout via safe.directory. Depois,
  # devolva .git ao usuário operacional `ubuntu` antes de retornar ao updater.
  #
  # Usamos somente raízes que existem no layout final. Isso cobre criações,
  # renomes e deleções da migração sem depender de `git ls-files`/pathspecs
  # opcionais, que foram o ponto fatal nas tentativas de recuperação anteriores.
  stage_errfile="$(mktemp "${TMPDIR:-/tmp}/dashboard-migrate-git.XXXXXX")"
  stage_rc=0
  stage_roots=()
  for rel in activity dashboard tests; do
    if [[ -e "$REPO_DIR/$rel" || -L "$REPO_DIR/$rel" ]] || \
       git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" cat-file -e "HEAD:$rel" 2>/dev/null; then
      stage_roots+=("$rel")
    fi
  done

  if (( ${#stage_roots[@]} == 0 )); then
    rm -f "$stage_errfile" 2>/dev/null || true
  elif [[ "$(id -u)" == "0" ]]; then
    if git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" add -A -- "${stage_roots[@]}" 2>"$stage_errfile"; then
      stage_rc=0
    else
      stage_rc=$?
    fi

    # O restante do updater da VPS usa git como ubuntu. Faça a devolução de
    # ownership apenas no checkout operacional (ou em worktree já pertencente
    # a ubuntu); repositórios temporários de testes devem manter seu owner.
    repo_owner="$(stat -c '%U' "$REPO_DIR" 2>/dev/null || true)"
    if id -u ubuntu >/dev/null 2>&1 && \
       [[ "$REPO_DIR" == "/home/ubuntu/bot" || "$repo_owner" == "ubuntu" ]]; then
      chown -R ubuntu:ubuntu "$REPO_DIR/.git" 2>/dev/null || true
      chmod -R u+rwX "$REPO_DIR/.git" 2>/dev/null || true
    fi
  elif (( ${#stage_roots[@]} > 0 )); then
    if git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" add -A -- "${stage_roots[@]}" 2>"$stage_errfile"; then
      stage_rc=0
    else
      stage_rc=$?
    fi
  fi

  if (( stage_rc != 0 )); then
    echo "falha ao stagear migração do dashboard (git rc=$stage_rc)" >&2
    cat "$stage_errfile" >&2 2>/dev/null || true
    rm -f "$stage_errfile" 2>/dev/null || true
    exit "$stage_rc"
  fi
  rm -f "$stage_errfile" 2>/dev/null || true
fi

echo "Layout finalizado em: $REPO_DIR/dashboard (testes do site em tests/site)"
