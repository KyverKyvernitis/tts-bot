#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
LEGACY_DIR="$REPO_DIR/activity "
TARGET_DIR="$REPO_DIR/activity"
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

if [[ ! -d "$REPO_DIR" ]]; then
  echo "repositório não encontrado: $REPO_DIR" >&2
  exit 1
fi

if [[ ! -e "$LEGACY_DIR" ]]; then
  echo "Diretório legado já não existe. Nada a migrar."
  exit 0
fi

if [[ "$MODE" != "apply" ]]; then
  echo "Migração necessária:"
  echo "  origem:  $LEGACY_DIR"
  echo "  destino: $TARGET_DIR"
  exit 3
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${ACTIVITY_PATH_BACKUP:-/home/ubuntu/activity-path-backup-$STAMP.tar.gz}"
mkdir -p "$(dirname "$BACKUP")"

tar -C "$REPO_DIR" -czf "$BACKUP" -- "activity "
echo "Backup criado: $BACKUP"

REPO_DIR="$REPO_DIR" LEGACY_DIR="$LEGACY_DIR" TARGET_DIR="$TARGET_DIR" python3 - <<'PY'
from __future__ import annotations

import filecmp
import os
import pathlib
import shutil
import stat

repo = pathlib.Path(os.environ["REPO_DIR"]).resolve()
legacy = pathlib.Path(os.environ["LEGACY_DIR"])
target = pathlib.Path(os.environ["TARGET_DIR"])

if legacy.resolve(strict=False).parent != repo:
    raise SystemExit(f"origem fora do repositório: {legacy}")
if target.resolve(strict=False).parent != repo:
    raise SystemExit(f"destino fora do repositório: {target}")
if legacy.is_symlink() or target.is_symlink():
    raise SystemExit("a migração não aceita diretórios raiz simbólicos")

# Diretórios gerados localmente não pertencem à migração. Eles são recriados por
# npm ci / npm run build depois que os fontes forem normalizados.
generated_dir_names = {"node_modules", "dist", ".vite", ".cache", "coverage"}
env_template_names = {".env.example", ".env.sample", ".env.template"}


def is_sensitive_env(path: pathlib.Path) -> bool:
    name = path.name
    return name == ".env" or (name.startswith(".env.") and name not in env_template_names)


def iter_source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root_raw, directories, filenames in os.walk(legacy, topdown=True, followlinks=False):
        root = pathlib.Path(root_raw)
        kept_directories: list[str] = []
        for name in directories:
            directory = root / name
            if name in generated_dir_names:
                continue
            if directory.is_symlink():
                raise SystemExit(f"symlink não permitido fora de diretório gerado: {directory}")
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in filenames:
            source = root / name
            if source.is_symlink():
                raise SystemExit(f"symlink não permitido fora de diretório gerado: {source}")
            mode = source.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SystemExit(f"arquivo especial não permitido no diretório legado: {source}")
            files.append(source)
    return files


source_files = iter_source_files()

# Valida todos os conflitos sensíveis antes de modificar qualquer arquivo.
for source in source_files:
    rel = source.relative_to(legacy)
    destination = target / rel
    if is_sensitive_env(source) and destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise SystemExit(f"conflito em arquivo sensível; resolva manualmente: {rel.as_posix()}")
        if not filecmp.cmp(source, destination, shallow=False):
            raise SystemExit(f"conflito em arquivo sensível; resolva manualmente: {rel.as_posix()}")

target.mkdir(parents=True, exist_ok=True)

moved = 0
removed = 0
preserved = 0
for source in sorted(source_files, key=lambda path: (len(path.parts), path.as_posix())):
    rel = source.relative_to(legacy)
    destination = target / rel
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not destination.exists():
        shutil.move(str(source), str(destination))
        moved += 1
        continue

    if is_sensitive_env(source):
        source.unlink()
        preserved += 1
        continue

    # O conteúdo do novo diretório é autoritativo para fontes entregues pelo
    # patch. A cópia antiga pode ser descartada porque o backup já foi criado.
    source.unlink()
    removed += 1

# Remove somente artefatos gerados conhecidos. Symlinks dentro de node_modules
# são normais e nunca são seguidos.
generated_removed = 0
for root_raw, directories, _filenames in os.walk(legacy, topdown=True, followlinks=False):
    root = pathlib.Path(root_raw)
    for name in list(directories):
        if name not in generated_dir_names:
            continue
        generated = root / name
        if generated.is_symlink():
            generated.unlink()
        else:
            shutil.rmtree(generated)
        directories.remove(name)
        generated_removed += 1

for root_raw, directories, _filenames in os.walk(legacy, topdown=False, followlinks=False):
    root = pathlib.Path(root_raw)
    for name in directories:
        directory = root / name
        if directory.is_symlink():
            raise SystemExit(f"symlink residual não permitido: {directory}")
        try:
            directory.rmdir()
        except OSError:
            pass

legacy.rmdir()

print(
    "Migração concluída: "
    f"movidos={moved} "
    f"antigos_descartados={removed} "
    f"sensíveis_preservados={preserved} "
    f"gerados_removidos={generated_removed}"
)
PY

if [[ -e "$LEGACY_DIR" ]]; then
  echo "ERRO: o diretório legado ainda existe após a migração." >&2
  exit 1
fi

if (( STAGE_GIT == 1 )); then
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "ERRO: --stage exige um repositório Git." >&2
    exit 1
  fi
  git -C "$REPO_DIR" add -A -- activity "activity "
  echo "Renomeação adicionada ao stage do Git."
fi

echo "Diretório normalizado para: $TARGET_DIR"
