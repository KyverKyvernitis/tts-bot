#!/usr/bin/env bash
# Preparação, isolamento, artefatos e promoção de candidatos locais.
# Carregado por atualizar.sh; não execute este módulo isoladamente.

preflight_local_candidate_permissions() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0

  local manifest="$LOCAL_CANDIDATE_DIR/manifest.json"
  local files_dir="${LOCAL_CANDIDATE_FILES_DIR:-$LOCAL_CANDIDATE_DIR/files}"
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-candidate-permissions.XXXXXX")"

  if sudo -u ubuntu -H env REPO_DIR="$REPO_DIR" MANIFEST_PATH="$manifest" FILES_DIR="$files_dir" python3 - <<'PYPREFLIGHTPERM' 2>"$errfile"
import json
import os
import pathlib
import sys

repo = pathlib.Path(os.environ['REPO_DIR']).resolve()
manifest = pathlib.Path(os.environ['MANIFEST_PATH']).resolve()
files_dir = pathlib.Path(os.environ['FILES_DIR']).resolve()

if not repo.is_dir() or not os.access(repo, os.R_OK | os.W_OK | os.X_OK):
    raise SystemExit(f'repositório não é acessível para o usuário do checkout: {repo}')
if not (repo / '.git').exists():
    raise SystemExit(f'checkout Git inválido ou .git inacessível: {repo}')
if not manifest.is_file() or not os.access(manifest, os.R_OK):
    raise SystemExit(f'manifesto do candidato não é legível: {manifest}')

try:
    data = json.loads(manifest.read_text(encoding='utf-8'))
except Exception as exc:
    raise SystemExit(f'manifesto do candidato não pôde ser lido: {type(exc).__name__}: {exc}')

def normalize_rel(raw):
    rel = pathlib.PurePosixPath(str(raw).replace('\\', '/'))
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido no candidato: {raw}')
    if not rel.parts:
        raise SystemExit(f'caminho vazio no candidato: {raw}')
    return rel

def resolve_inside(rel, *, label):
    dst = repo.joinpath(*rel.parts)
    try:
        resolved_dst = dst.resolve(strict=False)
    except Exception as exc:
        raise SystemExit(f'{label} não pôde ser resolvido: {rel.as_posix()}: {type(exc).__name__}')
    if resolved_dst != repo and repo not in resolved_dst.parents:
        raise SystemExit(f'{label} resolve para fora do repositório: {rel.as_posix()}')
    return dst

def writable_parent(dst, raw):
    parent = dst.parent
    while parent != repo and not parent.exists():
        parent = parent.parent
    if not parent.exists():
        parent = repo
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SystemExit(f'diretório pai não permite criar/alterar como ubuntu: {raw} (parent={parent})')

def validate_payload(raw):
    rel = normalize_rel(raw)
    src = files_dir.joinpath(*rel.parts)
    dst = resolve_inside(rel, label='destino')
    if not src.is_file() or not os.access(src, os.R_OK):
        raise SystemExit(f'arquivo do candidato não é legível como ubuntu: {raw}')
    if dst.is_symlink():
        raise SystemExit(f'destino é symlink e não pode ser sobrescrito: {raw}')
    if dst.exists() or dst.is_symlink():
        if not os.access(dst, os.W_OK):
            raise SystemExit(f'destino não é gravável como ubuntu: {raw}')
    else:
        writable_parent(dst, raw)

try:
    schema_version = int(data.get('schema_version') or 2)
except (TypeError, ValueError):
    raise SystemExit('schema_version inválido no candidato')

if schema_version >= 3:
    operations = data.get('operations') or []
    if not isinstance(operations, list) or not operations:
        raise SystemExit('manifesto v3 não contém operations válidas')
    for item in operations:
        if not isinstance(item, dict):
            raise SystemExit('operação declarativa inválida no candidato')
        op = str(item.get('op') or '').strip().lower()
        if op in {'add', 'update'}:
            validate_payload(item.get('path'))
        elif op == 'delete':
            rel = normalize_rel(item.get('path'))
            dst = resolve_inside(rel, label='alvo de delete')
            if dst.is_symlink():
                raise SystemExit(f'delete não aceita symlink: {rel.as_posix()}')
            # Em retomada o git rm pode já ter sido executado. A ausência será
            # validada contra o index em apply_local_candidate_operations().
            if dst.exists() and not dst.is_file():
                raise SystemExit(f'delete exige arquivo regular: {rel.as_posix()}')
            if dst.exists() and not os.access(dst.parent, os.W_OK | os.X_OK):
                raise SystemExit(f'diretório pai não permite delete como ubuntu: {rel.as_posix()}')
        elif op == 'move':
            source_rel = normalize_rel(item.get('from'))
            target_rel = normalize_rel(item.get('to'))
            source = resolve_inside(source_rel, label='origem de move')
            target = resolve_inside(target_rel, label='destino de move')
            if source.is_symlink() or target.is_symlink():
                raise SystemExit(f'move não aceita symlink: {source_rel.as_posix()} -> {target_rel.as_posix()}')
            if source.exists() and not source.is_file():
                raise SystemExit(f'move exige arquivo regular: {source_rel.as_posix()}')
            if source.exists() and target.exists():
                raise SystemExit(f'destino de move já existe: {target_rel.as_posix()}')
            # source ausente + target presente pode ser uma retomada após git mv.
            # O apply valida o index/hash antes de aceitar esse estado.
            if source.exists() and not os.access(source.parent, os.W_OK | os.X_OK):
                raise SystemExit(f'diretório pai não permite mover origem como ubuntu: {source_rel.as_posix()}')
            if not target.exists():
                writable_parent(target, target_rel.as_posix())
        else:
            raise SystemExit(f'operação declarativa desconhecida: {op or "<vazia>"}')
else:
    for raw in data.get('changed_files') or []:
        validate_payload(raw)

print('candidate-permissions-ok')
PYPREFLIGHTPERM
  then
    rm -f "$errfile" 2>/dev/null || true
    return 0
  fi

  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true
  [[ -n "${LAST_ERROR_STDERR//[[:space:]]/}" ]] || LAST_ERROR_STDERR="preflight de permissões do candidato falhou"
  LAST_ERROR_CODE="CANDIDATE_PERMISSION_DENIED"
  LAST_ERROR_COMMAND="preflight_local_candidate_permissions"
  return 1
}

discard_local_candidate_worktree() {
  [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" ]] || return 0
  if [[ -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]]; then
    repo_git worktree remove --force "$LOCAL_CANDIDATE_WORKTREE_DIR" >/dev/null 2>&1 \
      || rm -rf -- "$LOCAL_CANDIDATE_WORKTREE_DIR" 2>/dev/null \
      || true
  fi
  LOCAL_CANDIDATE_WORKTREE_DIR=""
}

create_local_candidate_worktree() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${CURRENT_COMMIT:-}" ]] || return 1

  discard_local_candidate_worktree || true
  # `prune_updater_runtime_orphans` já executa worktree prune dentro da
  # manutenção pesada horária. Repetir a poda aqui bloqueava toda criação de
  # candidato no caminho crítico sem aumentar a segurança transacional.

  local root safe_id target
  root="$CANDIDATE_ROOT/worktrees"
  safe_id="$(sanitize_update_component "${LOCAL_CANDIDATE_ID:-candidate}")"
  [[ -n "$safe_id" ]] || safe_id="candidate"
  target="$root/${safe_id}-${UPDATE_RUNTIME_RUN_ID}"

  mkdir -p "$root" || return 1
  chown ubuntu:ubuntu "$root" 2>/dev/null || true
  chmod 0775 "$root" 2>/dev/null || true
  rm -rf -- "$target" 2>/dev/null || true

  if ! repo_git worktree add --detach "$target" "$CURRENT_COMMIT" >/dev/null; then
    LAST_ERROR_STDERR="não foi possível criar worktree isolado para o candidato"
    LAST_ERROR_CODE="CANDIDATE_WORKTREE_CREATE_FAILED"
    return 1
  fi

  LOCAL_CANDIDATE_WORKTREE_DIR="$target"
  logger -t "$LOG_TAG" "worktree isolado criado para ${LOCAL_CANDIDATE_ID:-candidato}: $target" 2>/dev/null || true
  return 0
}

ensure_candidate_worktree_staged_clean() {
  [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]] || {
    LAST_ERROR_STDERR="worktree isolado do candidato não está disponível"
    LAST_ERROR_CODE="CANDIDATE_WORKTREE_MISSING"
    return 1
  }

  local unstaged untracked
  unstaged="$(candidate_git diff --name-only 2>/dev/null || true)"
  untracked="$(candidate_git ls-files --others --exclude-standard 2>/dev/null || true)"
  if [[ -n "${unstaged//[[:space:]]/}" || -n "${untracked//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="worktree do candidato contém mudanças fora do stage:
${unstaged:-}${unstaged:+$'\n'}${untracked:-}"
    LAST_ERROR_CODE="DIRTY_CANDIDATE_WORKTREE_AFTER_STAGE"
    return 1
  fi
  return 0
}

local_candidate_commit_body() {
  cat <<EOF
Candidate-ID: $LOCAL_CANDIDATE_ID
Update-ID: $LOCAL_CANDIDATE_DISPLAY_ID
Discord-Author-ID: ${LOCAL_CANDIDATE_SOURCE_AUTHOR_ID:-desconhecido}
Source-ZIP-SHA256: ${LOCAL_CANDIDATE_ZIP_SHA256:-indisponível}
EOF
}

candidate_commit_and_head() {
  # Commit + resolução do novo HEAD compartilham uma única fronteira sudo.
  # Mantemos todos os hooks Git e a política de assinatura do repositório;
  # apenas evitamos abrir um segundo `sudo -u ubuntu` para `rev-parse HEAD`.
  local subject="${1:?}" body="${2:?}" root
  root="$(candidate_repo_dir)"
  sudo -u ubuntu -H env GIT_EDITOR=: GIT_SEQUENCE_EDITOR=: \
    bash -c '
      set -e
      root="$1"
      subject="$2"
      body="$3"
      git -C "$root" commit --quiet -m "$subject" -m "$body" >/dev/null
      git -C "$root" rev-parse HEAD
    ' _ "$root" "$subject" "$body"
}

prepare_local_candidate_commit_in_worktree() {
  [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]] || return 1

  local op_started_ms commit_body
  STAGE="preflight do candidato em worktree"
  op_started_ms="$(update_now_ms)"
  if ! run_preflight_checks_in_dir "$LOCAL_CANDIDATE_WORKTREE_DIR"; then
    log_update_operation_timing_ms "preparation.static_preflight" "$op_started_ms"
    LAST_ERROR_STDERR="preflight estático falhou no worktree isolado"
    LAST_ERROR_CODE="CANDIDATE_WORKTREE_PREFLIGHT_FAILED"
    return 1
  fi
  log_update_operation_timing_ms "preparation.static_preflight" "$op_started_ms"

  op_started_ms="$(update_now_ms)"
  if ! ensure_candidate_worktree_staged_clean; then
    log_update_operation_timing_ms "preparation.worktree_clean_check" "$op_started_ms"
    return 1
  fi
  log_update_operation_timing_ms "preparation.worktree_clean_check" "$op_started_ms"

  if candidate_git diff --cached --quiet; then
    LAST_ERROR_STDERR="candidato não produziu diff staged no worktree isolado"
    LAST_ERROR_CODE="EMPTY_CANDIDATE_DIFF"
    return 1
  fi

  # A validação isolada terminou de verdade aqui. A fase seguinte inclui commit,
  # preparação de artefatos e snapshot de rollback até o candidato ficar READY.
  if declare -F zip_progress_done_and_publish >/dev/null 2>&1; then
    zip_progress_done_and_publish "Candidato validado" "Criando release candidata"
  fi

  # O marcador `commit` antigo também incluía tudo desde o último timing
  # grosseiro (segurança, worktree, stage e preflight), o que fazia um commit
  # rápido parecer levar 10–15s. Feche essa janela como `prepare` antes de
  # medir o commit de verdade.
  mark_update_timing "prepare"

  STAGE="commit isolado do candidato"
  commit_body="$(local_candidate_commit_body)"
  local commit_started_ms commit_finished_ms commit_elapsed_ms state_started_ms
  commit_started_ms="$(update_now_ms)"
  if ! LOCAL_CANDIDATE_PREPARED_COMMIT="$(candidate_commit_and_head "$LOCAL_CANDIDATE_COMMIT_MESSAGE" "$commit_body")"; then
    log_update_operation_timing_ms "preparation.commit.command_and_head" "$commit_started_ms"
    log_update_operation_timing_ms "preparation.commit" "$commit_started_ms"
    commit_finished_ms="$(update_now_ms)"
    commit_elapsed_ms=$((commit_finished_ms - commit_started_ms))
    (( commit_elapsed_ms < 0 )) && commit_elapsed_ms=0
    append_update_timing_ms "commit" "$commit_elapsed_ms"
    UPDATER_STEP_LAST="$SECONDS"
    LAST_ERROR_STDERR="não foi possível criar commit isolado do candidato"
    LAST_ERROR_CODE="CANDIDATE_WORKTREE_COMMIT_FAILED"
    return 1
  fi
  commit_finished_ms="$(update_now_ms)"
  commit_elapsed_ms=$((commit_finished_ms - commit_started_ms))
  (( commit_elapsed_ms < 0 )) && commit_elapsed_ms=0
  log_update_operation_timing_ms "preparation.commit.command_and_head" "$commit_started_ms"
  log_update_operation_timing_ms "preparation.commit" "$commit_started_ms"
  append_update_timing_ms "commit" "$commit_elapsed_ms"
  # O próximo timing grosseiro deve começar depois do commit real, não depois
  # do bloco de preparação anterior.
  UPDATER_STEP_LAST="$SECONDS"

  state_started_ms="$(update_now_ms)"
  write_local_candidate_state "prepared" "$LOCAL_CANDIDATE_PREPARED_COMMIT"
  log_update_operation_timing_ms "preparation.commit.state_write" "$state_started_ms"
  logger -t "$LOG_TAG" "candidato ${LOCAL_CANDIDATE_ID:-desconhecido} preparado isoladamente em $(short_commit "$LOCAL_CANDIDATE_PREPARED_COMMIT")" 2>/dev/null || true
  return 0
}

local_candidate_artifact_root_for_commit() {
  local commit="${1:-${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}}"
  [[ -n "$commit" ]] || return 1
  if [[ "${LOCAL_CANDIDATE_MODE:-0}" == "1" ]]; then
    [[ -n "${LOCAL_CANDIDATE_DIR:-}" ]] || return 1
    printf '%s/runtime-artifacts/%s\n' "$LOCAL_CANDIDATE_DIR" "$commit"
    return 0
  fi
  if [[ "${REMOTE_CANDIDATE_MODE:-0}" == "1" ]]; then
    printf '%s/%s\n' "$REMOTE_RUNTIME_ARTIFACT_ROOT" "$commit"
    return 0
  fi
  # Compatibilidade dos harnesses/testes que chamam o helper isoladamente.
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" ]] || return 1
  printf '%s/runtime-artifacts/%s\n' "$LOCAL_CANDIDATE_DIR" "$commit"
}

hydrate_local_candidate_runtime_artifacts() {
  local commit="${1:-${LOCAL_CANDIDATE_PREPARED_COMMIT:-}}" root ready
  [[ -n "$commit" ]] || return 1
  root="$(local_candidate_artifact_root_for_commit "$commit")" || return 1
  ready="$root/ready.json"
  [[ -s "$ready" && ! -L "$ready" ]] || return 1

  if ! ARTIFACT_READY_FILE="$ready" ARTIFACT_EXPECTED_COMMIT="$commit" python3 - <<'PYARTIFACTREADY' >/dev/null 2>&1
import json, os, pathlib
path = pathlib.Path(os.environ['ARTIFACT_READY_FILE'])
data = json.loads(path.read_text(encoding='utf-8'))
if str(data.get('commit') or '') != os.environ['ARTIFACT_EXPECTED_COMMIT']:
    raise SystemExit(1)
if data.get('state') != 'ready':
    raise SystemExit(1)
PYARTIFACTREADY
  then
    return 1
  fi

  LOCAL_CANDIDATE_ARTIFACT_ROOT="$root"
  LOCAL_CANDIDATE_ARTIFACT_COMMIT="$commit"
  if [[ "${REMOTE_CANDIDATE_MODE:-0}" == "1" ]]; then
    REMOTE_CANDIDATE_ARTIFACT_ROOT="$root"
  fi
  LOCAL_CANDIDATE_FRONTEND_ARTIFACT=""
  LOCAL_CANDIDATE_BACKEND_ARTIFACT=""
  LOCAL_CANDIDATE_BACKEND_DEP_KEY=""
  LOCAL_CANDIDATE_BACKEND_DEP_LAYER=""
  LOCAL_CANDIDATE_PYTHON_ARTIFACT=""
  LOCAL_CANDIDATE_PYTHON_READY=0
  if [[ -s "$root/frontend/dist/index.html" ]]; then
    LOCAL_CANDIDATE_FRONTEND_ARTIFACT="$root/frontend/dist"
  fi
  if [[ -s "$root/backend/dist/index.js" && -s "$root/backend/deps.json" ]]; then
    local backend_dep_key backend_dep_layer
    backend_dep_key="$(ARTIFACT_DEPS_FILE="$root/backend/deps.json" python3 - <<'PYBACKDEPS' 2>/dev/null
import json, os
with open(os.environ['ARTIFACT_DEPS_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
key = str(data.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKDEPS
)" || return 1
    backend_dep_layer="$(node_dependency_layer_root backend prod "$backend_dep_key")" || return 1
    verify_node_dependency_layer "$backend_dep_layer" "$backend_dep_key" prod || return 1
    LOCAL_CANDIDATE_BACKEND_ARTIFACT="$root/backend"
    LOCAL_CANDIDATE_BACKEND_DEP_KEY="$backend_dep_key"
    LOCAL_CANDIDATE_BACKEND_DEP_LAYER="$backend_dep_layer"
  fi
  if ARTIFACT_READY_FILE="$ready" python3 - <<'PYARTIFACTPYREADY' >/dev/null 2>&1
import json, os
with open(os.environ['ARTIFACT_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('python') if isinstance(data, dict) else None
raise SystemExit(0 if isinstance(record, dict) and record.get('ready') else 1)
PYARTIFACTPYREADY
  then
    LOCAL_CANDIDATE_PYTHON_ARTIFACT="$(python_runtime_release_root_for_commit "$commit")" || return 1
    if ! verify_python_runtime_release_for_commit "$LOCAL_CANDIDATE_PYTHON_ARTIFACT" "$commit"; then
      return 1
    fi
    LOCAL_CANDIDATE_PYTHON_READY=1
  fi
  LOCAL_CANDIDATE_RUNTIME_READY=1
  return 0
}

verify_local_candidate_artifact_integrity() {
  local kind="${1:?}" root="${LOCAL_CANDIDATE_ARTIFACT_ROOT:-}" ready
  if [[ "$kind" == "python" ]]; then
    [[ -n "${LOCAL_CANDIDATE_PYTHON_ARTIFACT:-}" ]] || return 1
    local install_requirements
    install_requirements="$(python_runtime_install_requirements_file "$REPO_DIR" || true)"
    [[ -n "$install_requirements" ]] || return 1
    verify_python_runtime_release "$LOCAL_CANDIDATE_PYTHON_ARTIFACT" "${LOCAL_CANDIDATE_ARTIFACT_COMMIT:-${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}}" "$install_requirements"
    return $?
  fi
  [[ -n "$root" ]] || return 1
  ready="$root/ready.json"
  [[ -s "$ready" ]] || return 1

  if [[ "$kind" == "backend" ]]; then
    local deps_key deps_layer
    deps_key="$(ARTIFACT_READY_FILE="$ready" python3 - <<'PYBACKREADY' 2>/dev/null
import json, os
with open(os.environ['ARTIFACT_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('backend') if isinstance(data, dict) else None
if not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
key = str(record.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKREADY
)" || return 1
    deps_layer="$(node_dependency_layer_root backend prod "$deps_key")" || return 1
    verify_node_dependency_layer "$deps_layer" "$deps_key" prod || return 1
  fi

  ARTIFACT_READY_FILE="$ready" ARTIFACT_KIND="$kind" ARTIFACT_ROOT="$root" python3 - <<'PYVERIFYARTIFACT' >/dev/null 2>&1
import hashlib, json, os, pathlib
ready = pathlib.Path(os.environ['ARTIFACT_READY_FILE'])
kind = os.environ['ARTIFACT_KIND']
root = pathlib.Path(os.environ['ARTIFACT_ROOT'])
data = json.loads(ready.read_text(encoding='utf-8'))
record = data.get(kind) if isinstance(data, dict) else None
if not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
path = root / ('frontend/dist' if kind == 'frontend' else 'backend/dist')

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode('utf-8') + b'\0' + target.encode('utf-8') + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode('utf-8') + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if not path.exists() or tree_hash(path) != str(record.get('sha256') or ''):
    raise SystemExit(1)
PYVERIFYARTIFACT
}

write_local_candidate_artifact_ready_manifest() {
  local root="${1:?}" commit="${2:?}" front_ready="${3:-0}" back_ready="${4:-0}" python_ready="${5:-0}"
  ARTIFACT_ROOT="$root" ARTIFACT_COMMIT="$commit" ARTIFACT_FRONT_READY="$front_ready" ARTIFACT_BACK_READY="$back_ready" ARTIFACT_PYTHON_READY="$python_ready" \
    python3 - <<'PYARTIFACTMANIFEST'
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['ARTIFACT_ROOT'])

def tree_hash(path: pathlib.Path) -> str:
    if not path.exists():
        return ''
    digest = hashlib.sha256()
    base = path.resolve()
    for item in sorted(path.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(f'artifact contains absolute symlink: {rel}')
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(base)
            except ValueError:
                raise SystemExit(f'artifact symlink escapes root: {rel}')
            digest.update(b'L\0' + rel.encode('utf-8') + b'\0' + target.encode('utf-8') + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode('utf-8') + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

back_ready = os.environ.get('ARTIFACT_BACK_READY') == '1'
back_deps_key = ''
if back_ready:
    deps_file = root / 'backend' / 'deps.json'
    if not deps_file.is_file() or deps_file.is_symlink():
        raise SystemExit('backend artifact missing deps.json')
    deps = json.loads(deps_file.read_text(encoding='utf-8'))
    back_deps_key = str(deps.get('deps_key') or '')
    if len(back_deps_key) != 64:
        raise SystemExit('backend artifact has invalid deps_key')

payload = {
    'state': 'ready',
    'commit': os.environ['ARTIFACT_COMMIT'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'frontend': {
        'ready': os.environ.get('ARTIFACT_FRONT_READY') == '1',
        'sha256': tree_hash(root / 'frontend' / 'dist') if os.environ.get('ARTIFACT_FRONT_READY') == '1' else '',
    },
    'backend': {
        'ready': back_ready,
        'sha256': tree_hash(root / 'backend' / 'dist') if back_ready else '',
        'deps_key': back_deps_key,
    },
    'python': {
        'ready': os.environ.get('ARTIFACT_PYTHON_READY') == '1',
        'release_commit': os.environ['ARTIFACT_COMMIT'] if os.environ.get('ARTIFACT_PYTHON_READY') == '1' else '',
    },
}
path = root / 'ready.json'
tmp = path.with_name('.ready.json.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYARTIFACTMANIFEST
}

python_runtime_install_requirements_file() {
  local root="${1:?}"
  if [[ -s "$root/requirements.lock" && ! -L "$root/requirements.lock" ]]; then
    printf '%s\n' "$root/requirements.lock"
    return 0
  fi
  [[ -f "$root/requirements.txt" && ! -L "$root/requirements.txt" ]] || return 1
  printf '%s\n' "$root/requirements.txt"
}

python_runtime_base_python() {
  if [[ -x /usr/bin/python3 ]]; then
    printf '%s\n' /usr/bin/python3
    return 0
  fi
  command -v python3 2>/dev/null || return 1
}

python_uv_tool_root() {
  printf '%s/uv-%s\n' "${PYTHON_TOOL_ROOT:-$CANDIDATE_ROOT/python-tools}" "${PYTHON_UV_VERSION:-0.12.13}"
}

python_uv_bin_if_ready() {
  local candidate version expected="${PYTHON_UV_VERSION:-0.12.13}"
  if [[ -n "${TTS_BOT_UV_BIN:-}" && -x "${TTS_BOT_UV_BIN}" ]]; then
    candidate="$TTS_BOT_UV_BIN"
  elif [[ -n "${PYTHON_UV_BIN_RESOLVED:-}" && -x "${PYTHON_UV_BIN_RESOLVED}" ]]; then
    candidate="$PYTHON_UV_BIN_RESOLVED"
  elif [[ -x /home/ubuntu/.local/bin/uv ]]; then
    candidate=/home/ubuntu/.local/bin/uv
  elif [[ -x /usr/local/bin/uv ]]; then
    candidate=/usr/local/bin/uv
  elif [[ -x /usr/bin/uv ]]; then
    candidate=/usr/bin/uv
  else
    candidate="$(python_uv_tool_root)/venv/bin/uv"
  fi
  [[ -x "$candidate" ]] || return 1
  version="$(sudo -u ubuntu -H "$candidate" --version 2>/dev/null | awk 'NR==1{print $2}')"
  [[ "$version" == "$expected" ]] || return 1
  PYTHON_UV_BIN_RESOLVED="$candidate"
  printf '%s\n' "$candidate"
}

ensure_python_uv_tool() {
  local mode="${PYTHON_INSTALLER_MODE:-auto}" base_py="${1:-}" root command rc
  case "$mode" in
    pip) return 1 ;;
    auto|uv) ;;
    *)
      LAST_ERROR_STDERR="TTS_BOT_PYTHON_INSTALLER inválido: $mode (use auto, uv ou pip)"
      LAST_ERROR_CODE="PYTHON_INSTALLER_MODE_INVALID"
      return 2
      ;;
  esac
  if python_uv_bin_if_ready >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${PYTHON_AUTO_BOOTSTRAP_UV:-1}" != "1" ]]; then
    [[ "$mode" == uv ]] && return 2 || return 1
  fi
  [[ -n "$base_py" && -x "$base_py" ]] || base_py="$(python_runtime_base_python || true)"
  [[ -x "$base_py" ]] || return 2
  root="$(python_uv_tool_root)"
  rm -rf -- "$root.tmp" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$(dirname "$root")" || return 2
  STAGE="bootstrap do instalador Python"
  printf -v command '%q -m venv %q && %q -m pip install --disable-pip-version-check --prefer-binary --no-input --progress-bar off %q' \
    "$base_py" "$root.tmp/venv" "$root.tmp/venv/bin/python" "uv==${PYTHON_UV_VERSION:-0.12.13}"
  if zip_progress_run_as_ubuntu \
    "Preparando instalador Python rápido" \
    "Instalando uv ${PYTHON_UV_VERSION:-0.12.13} uma única vez" \
    "$command"
  then
    :
  else
    rc=$?
    rm -rf -- "$root.tmp" 2>/dev/null || true
    if [[ "$mode" == uv ]]; then
      register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-bootstrap uv}"
      LAST_ERROR_CODE="PYTHON_UV_BOOTSTRAP_FAILED"
      return 2
    fi
    logger -t "$LOG_TAG" "uv indisponível; fallback seguro para pip" 2>/dev/null || true
    return 1
  fi
  if [[ ! -x "$root.tmp/venv/bin/uv" || "$(sudo -u ubuntu -H "$root.tmp/venv/bin/uv" --version 2>/dev/null | awk 'NR==1{print $2}')" != "${PYTHON_UV_VERSION:-0.12.13}" ]]; then
    rm -rf -- "$root.tmp" 2>/dev/null || true
    [[ "$mode" == uv ]] && return 2 || return 1
  fi
  rm -rf -- "$root" 2>/dev/null || true
  mv -- "$root.tmp" "$root" || return 2
  PYTHON_UV_BIN_RESOLVED="$root/venv/bin/uv"
  return 0
}

prepare_python_runtime_with_uv() {
  local uv="${1:?}" base_py="${2:?}" root="${3:?}" requirements="${4:?}" command
  install -d -o ubuntu -g ubuntu -m 0775 "${PYTHON_UV_CACHE_ROOT:-$CANDIDATE_ROOT/uv-cache}" || return 1
  printf -v command 'UV_CACHE_DIR=%q UV_LINK_MODE=clone UV_NO_PROGRESS=1 UV_NO_PYTHON_DOWNLOADS=1 UV_NO_MANAGED_PYTHON=1 UV_NO_CONFIG=1 %q venv --python %q --seed %q && UV_CACHE_DIR=%q UV_LINK_MODE=clone UV_NO_PROGRESS=1 UV_NO_PYTHON_DOWNLOADS=1 UV_NO_MANAGED_PYTHON=1 UV_NO_CONFIG=1 %q pip install --python %q -r %q && %q -m pip check' \
    "${PYTHON_UV_CACHE_ROOT:-$CANDIDATE_ROOT/uv-cache}" "$uv" "$base_py" "$root/venv" \
    "${PYTHON_UV_CACHE_ROOT:-$CANDIDATE_ROOT/uv-cache}" "$uv" "$root/venv/bin/python" "$requirements" "$root/venv/bin/python"
  zip_progress_run_as_ubuntu \
    "Preparando dependências Python" \
    "Instalando ambiente isolado via uv/cache CoW" \
    "$command"
}

prepare_python_runtime_with_pip() {
  local base_py="${1:?}" root="${2:?}" requirements="${3:?}" command
  printf -v command '%q -m venv %q && %q -m pip install --disable-pip-version-check --prefer-binary --no-input --progress-bar off -r %q && %q -m pip check' \
    "$base_py" "$root/venv" "$root/venv/bin/python" "$requirements" "$root/venv/bin/python"
  zip_progress_run_as_ubuntu \
    "Preparando dependências Python" \
    "Criando ambiente isolado do candidato via pip/cache" \
    "$command"
}

python_runtime_release_root_for_commit() {
  local commit
  commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" ]] || return 1
  printf '%s/%s\n' "$PYTHON_RUNTIME_ROOT" "$commit"
}

python_runtime_freeze_hash() {
  local root="${1:?}" py="$root/venv/bin/python"
  [[ -x "$py" ]] || return 1
  sudo -u ubuntu -H "$py" -m pip freeze --all 2>/dev/null | LC_ALL=C sort | sha256sum | awk '{print $1}'
}

write_python_runtime_manifest() {
  local root="${1:?}" commit="${2:?}" requirements_file="${3:-}"
  local py="$root/venv/bin/python" freeze_hash requirements_hash python_version
  [[ -x "$py" ]] || return 1
  freeze_hash="$(python_runtime_freeze_hash "$root")" || return 1
  requirements_hash=""
  if [[ -n "$requirements_file" && -f "$requirements_file" ]]; then
    requirements_hash="$(sha256sum "$requirements_file" | awk '{print $1}')"
  fi
  python_version="$(sudo -u ubuntu -H "$py" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
  PY_RUNTIME_ROOT="$root" PY_RUNTIME_COMMIT="$commit" PY_RUNTIME_FREEZE_SHA="$freeze_hash" \
  PY_RUNTIME_REQUIREMENTS_SHA="$requirements_hash" PY_RUNTIME_PYTHON_VERSION="$python_version" \
  PY_RUNTIME_INSTALLER="${PYTHON_INSTALLER_STATUS:-unknown}" PY_RUNTIME_REQUIREMENTS_NAME="$(basename "${requirements_file:-requirements.txt}")" \
    python3 - <<'PYPYMANIFEST'
import datetime, json, os, pathlib
root = pathlib.Path(os.environ['PY_RUNTIME_ROOT'])
payload = {
    'state': 'ready',
    'commit': os.environ['PY_RUNTIME_COMMIT'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'python_version': os.environ.get('PY_RUNTIME_PYTHON_VERSION') or '',
    'requirements_sha256': os.environ.get('PY_RUNTIME_REQUIREMENTS_SHA') or '',
    'requirements_file': os.environ.get('PY_RUNTIME_REQUIREMENTS_NAME') or 'requirements.txt',
    'installer': os.environ.get('PY_RUNTIME_INSTALLER') or 'unknown',
    'freeze_sha256': os.environ['PY_RUNTIME_FREEZE_SHA'],
}
path = root / 'python.json'
tmp = root / '.python.json.tmp'
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYPYMANIFEST
}

python_runtime_requirements_hash_for_commit() {
  local commit="${1:?}" path
  if repo_git cat-file -e "${commit}:requirements.lock" 2>/dev/null; then
    path="requirements.lock"
  elif repo_git cat-file -e "${commit}:requirements.txt" 2>/dev/null; then
    path="requirements.txt"
  else
    return 1
  fi
  repo_git show "${commit}:${path}" 2>/dev/null | sha256sum | awk '{print $1}'
}

python_runtime_manifest_matches_requirements_hash() {
  local root="${1:?}" expected_hash="${2:?}" manifest="$root/python.json"
  [[ -s "$manifest" && ! -L "$manifest" ]] || return 1
  PY_RUNTIME_MANIFEST="$manifest" PY_RUNTIME_REQUIREMENTS_SHA="$expected_hash" python3 - <<'PYPYMANIFESTREQ' >/dev/null 2>&1
import json, os
with open(os.environ['PY_RUNTIME_MANIFEST'], encoding='utf-8') as fh:
    data = json.load(fh)
raise SystemExit(0 if data.get('requirements_sha256') == os.environ['PY_RUNTIME_REQUIREMENTS_SHA'] else 1)
PYPYMANIFESTREQ
}

verify_python_runtime_release_for_commit() {
  local root="${1:?}" commit="${2:?}" expected_hash
  verify_python_runtime_release "$root" "$commit" || return 1
  expected_hash="$(python_runtime_requirements_hash_for_commit "$commit" || true)"
  [[ -n "$expected_hash" ]] || return 1
  python_runtime_manifest_matches_requirements_hash "$root" "$expected_hash"
}

verify_python_runtime_release() {
  local root="${1:?}" expected_commit="${2:?}" requirements_file="${3:-}"
  local manifest="$root/python.json" py="$root/venv/bin/python" freeze_hash requirements_hash=""
  [[ -s "$manifest" && ! -L "$manifest" && -x "$py" ]] || return 1
  if ! PY_RUNTIME_MANIFEST="$manifest" PY_RUNTIME_EXPECTED_COMMIT="$expected_commit" python3 - <<'PYPYVERIFY' >/dev/null 2>&1
import json, os, pathlib
path = pathlib.Path(os.environ['PY_RUNTIME_MANIFEST'])
data = json.loads(path.read_text(encoding='utf-8'))
if data.get('state') != 'ready':
    raise SystemExit(1)
if str(data.get('commit') or '') != os.environ['PY_RUNTIME_EXPECTED_COMMIT']:
    raise SystemExit(1)
if not str(data.get('freeze_sha256') or ''):
    raise SystemExit(1)
PYPYVERIFY
  then
    return 1
  fi
  freeze_hash="$(python_runtime_freeze_hash "$root")" || return 1
  PY_RUNTIME_MANIFEST="$manifest" PY_RUNTIME_FREEZE_SHA="$freeze_hash" python3 - <<'PYPYFREEZE' >/dev/null 2>&1 || return 1
import json, os
with open(os.environ['PY_RUNTIME_MANIFEST'], encoding='utf-8') as fh:
    data = json.load(fh)
raise SystemExit(0 if data.get('freeze_sha256') == os.environ['PY_RUNTIME_FREEZE_SHA'] else 1)
PYPYFREEZE
  if [[ -n "$requirements_file" && -f "$requirements_file" ]]; then
    requirements_hash="$(sha256sum "$requirements_file" | awk '{print $1}')"
    PY_RUNTIME_MANIFEST="$manifest" PY_RUNTIME_REQUIREMENTS_SHA="$requirements_hash" python3 - <<'PYPYREQ' >/dev/null 2>&1 || return 1
import json, os
with open(os.environ['PY_RUNTIME_MANIFEST'], encoding='utf-8') as fh:
    data = json.load(fh)
raise SystemExit(0 if data.get('requirements_sha256') == os.environ['PY_RUNTIME_REQUIREMENTS_SHA'] else 1)
PYPYREQ
  fi
  sudo -u ubuntu -H "$py" -m pip check >/dev/null 2>&1
}

prepare_candidate_python_runtime() {
  local validation_root="${1:?}" commit="${2:?}" requirements
  requirements="$(python_runtime_install_requirements_file "$validation_root" || true)"
  [[ -n "$requirements" && -f "$requirements" ]] || {
    LAST_ERROR_STDERR="requirements.txt/requirements.lock ausente no candidato"
    LAST_ERROR_CODE="PYTHON_REQUIREMENTS_MISSING"
    return 1
  }
  local root py base_py uv="" rc=0
  root="$(python_runtime_release_root_for_commit "$commit")" || return 1
  if verify_python_runtime_release "$root" "$commit" "$requirements"; then
    LOCAL_CANDIDATE_PYTHON_ARTIFACT="$root"
    LOCAL_CANDIDATE_PYTHON_READY=1
    PYTHON_INSTALLER_STATUS="runtime hit"
    return 0
  fi
  if [[ -L "$PYTHON_RUNTIME_CURRENT_LINK" && "$(readlink -f "$PYTHON_RUNTIME_CURRENT_LINK" 2>/dev/null || true)" == "$(readlink -f "$root" 2>/dev/null || true)" ]]; then
    LAST_ERROR_STDERR="release Python candidato ativo falhou na verificação; recusando apagar runtime em uso"
    LAST_ERROR_CODE="PYTHON_RUNTIME_ACTIVE_INVALID"
    return 1
  fi

  rm -rf -- "$root" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$root" || {
    LAST_ERROR_STDERR="não foi possível criar release Python candidato: $root"
    LAST_ERROR_CODE="PYTHON_RUNTIME_DIR_FAILED"
    return 1
  }
  base_py="$(python_runtime_base_python || true)"
  [[ -n "$base_py" && -x "$base_py" ]] || {
    LAST_ERROR_STDERR="python3 indisponível para criar ambiente candidato"
    LAST_ERROR_CODE="PYTHON_RUNTIME_BASE_MISSING"
    return 1
  }
  STAGE="ambiente Python do candidato"

  set +e
  ensure_python_uv_tool "$base_py"
  rc=$?
  set -e
  uv="${PYTHON_UV_BIN_RESOLVED:-}"
  if (( rc == 0 )) && [[ -n "$uv" ]]; then
    if prepare_python_runtime_with_uv "$uv" "$base_py" "$root" "$requirements"; then
      PYTHON_INSTALLER_STATUS="uv ${PYTHON_UV_VERSION:-0.12.13}"
    else
      rc=$?
      register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-uv venv / uv pip install}"
      LAST_ERROR_CODE="PYTHON_RUNTIME_PREPARE_FAILED"
      rm -rf -- "$root" 2>/dev/null || true
      return "$rc"
    fi
  elif (( rc == 2 )) && [[ "${PYTHON_INSTALLER_MODE:-auto}" == uv ]]; then
    LAST_ERROR_STDERR="uv foi exigido, mas não pôde ser preparado"
    LAST_ERROR_CODE="PYTHON_UV_REQUIRED_UNAVAILABLE"
    rm -rf -- "$root" 2>/dev/null || true
    return 1
  else
    if prepare_python_runtime_with_pip "$base_py" "$root" "$requirements"; then
      PYTHON_INSTALLER_STATUS="pip"
    else
      rc=$?
      register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-python3 -m venv / pip install}"
      LAST_ERROR_CODE="PYTHON_RUNTIME_PREPARE_FAILED"
      rm -rf -- "$root" 2>/dev/null || true
      return "$rc"
    fi
  fi

  if ! write_python_runtime_manifest "$root" "$commit" "$requirements"; then
    LAST_ERROR_STDERR="ambiente Python criado, mas manifesto imutável não pôde ser gravado"
    LAST_ERROR_CODE="PYTHON_RUNTIME_MANIFEST_FAILED"
    rm -rf -- "$root" 2>/dev/null || true
    return 1
  fi
  if ! verify_python_runtime_release "$root" "$commit" "$requirements"; then
    LAST_ERROR_STDERR="ambiente Python candidato falhou na verificação pós-instalação"
    LAST_ERROR_CODE="PYTHON_RUNTIME_VERIFY_FAILED"
    rm -rf -- "$root" 2>/dev/null || true
    return 1
  fi
  chown ubuntu:ubuntu "$root/python.json" 2>/dev/null || true
  chmod 0644 "$root/python.json" 2>/dev/null || true
  LOCAL_CANDIDATE_PYTHON_ARTIFACT="$root"
  LOCAL_CANDIDATE_PYTHON_READY=1
  return 0
}

capture_python_runtime_release_snapshot() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")"
  (( ${REQUIREMENTS_CHANGED:-0} == 1 )) || return 0
  [[ -n "$commit" ]] || return 1
  local root current_py source_venv requirements_file
  requirements_file="$(python_runtime_install_requirements_file "$REPO_DIR" || true)"
  [[ -n "$requirements_file" ]] || return 1
  root="$(python_runtime_release_root_for_commit "$commit")" || return 1
  if verify_python_runtime_release "$root" "$commit" "$requirements_file"; then
    return 0
  fi
  current_py="$(current_bot_python_bin)"
  [[ -x "$current_py" ]] || {
    LAST_ERROR_STDERR="runtime Python saudável atual não foi encontrado para snapshot"
    LAST_ERROR_CODE="PYTHON_RUNTIME_BASELINE_MISSING"
    return 1
  }
  source_venv="$(dirname "$(dirname "$current_py")")"
  [[ -d "$source_venv" ]] || return 1
  if [[ -L "$PYTHON_RUNTIME_CURRENT_LINK" && "$(readlink -f "$PYTHON_RUNTIME_CURRENT_LINK" 2>/dev/null || true)" == "$(readlink -f "$root" 2>/dev/null || true)" ]]; then
    LAST_ERROR_STDERR="runtime Python ativo do commit anterior está inconsistente; recusando sobrescrever baseline em uso"
    LAST_ERROR_CODE="PYTHON_RUNTIME_ACTIVE_BASELINE_INVALID"
    return 1
  fi
  rm -rf -- "$root" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$root" || return 1
  if ! sudo -u ubuntu -H cp -a -- "$source_venv" "$root/venv"; then
    LAST_ERROR_STDERR="falha ao preservar ambiente Python anterior para rollback"
    LAST_ERROR_CODE="PYTHON_RUNTIME_SNAPSHOT_FAILED"
    rm -rf -- "$root" 2>/dev/null || true
    return 1
  fi
  if ! write_python_runtime_manifest "$root" "$commit" "$requirements_file" || ! verify_python_runtime_release "$root" "$commit" "$requirements_file"; then
    LAST_ERROR_STDERR="snapshot do runtime Python anterior não passou na verificação"
    LAST_ERROR_CODE="PYTHON_RUNTIME_SNAPSHOT_INVALID"
    rm -rf -- "$root" 2>/dev/null || true
    return 1
  fi
  chown ubuntu:ubuntu "$root/python.json" 2>/dev/null || true
  chmod 0644 "$root/python.json" 2>/dev/null || true
  logger -t "$LOG_TAG" "runtime Python anterior preservado para rollback: $(short_commit "$commit")" 2>/dev/null || true
  return 0
}

activate_python_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-}")" root tmp
  [[ -n "$commit" ]] || return 1
  root="$(python_runtime_release_root_for_commit "$commit")" || return 1
  local install_requirements
  install_requirements="$(python_runtime_install_requirements_file "$REPO_DIR" || true)"
  if [[ -z "$install_requirements" ]] || ! verify_python_runtime_release "$root" "$commit" "$install_requirements"; then
    LAST_ERROR_STDERR="release Python ausente, inconsistente ou incompatível com requirements.txt: $(short_commit "$commit")"
    LAST_ERROR_CODE="PYTHON_RUNTIME_RELEASE_INVALID"
    return 1
  fi
  install -d -o ubuntu -g ubuntu -m 0775 "$PYTHON_RUNTIME_ROOT" || return 1
  tmp="$PYTHON_RUNTIME_ROOT/.current.$$.${RANDOM:-0}"
  rm -f -- "$tmp" 2>/dev/null || true
  ln -s -- "$root" "$tmp" || return 1
  if ! mv -Tf -- "$tmp" "$PYTHON_RUNTIME_CURRENT_LINK"; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  PYTHON_RUNTIME_MUTATED=1
  return 0
}

restore_python_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")"
  if ! activate_python_runtime_release "$commit"; then
    LAST_ERROR_CODE="ROLLBACK_PYTHON_RUNTIME_INVALID"
    return 1
  fi
  # A restauração não é uma mutação da versão nova; evita reentrância confusa.
  PYTHON_RUNTIME_MUTATED=0
  return 0
}

prune_python_runtime_releases() {
  local keep="${PYTHON_RUNTIME_RETENTION:-3}" current_target="" count=0 entry
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=3
  (( keep >= 1 )) || keep=1
  [[ -d "$PYTHON_RUNTIME_ROOT" ]] || return 0
  if [[ -L "$PYTHON_RUNTIME_CURRENT_LINK" ]]; then
    current_target="$(readlink -f "$PYTHON_RUNTIME_CURRENT_LINK" 2>/dev/null || true)"
  fi
  while IFS= read -r entry; do
    [[ -n "$entry" && -d "$entry" ]] || continue
    [[ "$entry" == "$current_target" ]] && continue
    count=$((count + 1))
    if (( count > keep )); then
      rm -rf -- "$entry" 2>/dev/null || true
    fi
  done < <(find "$PYTHON_RUNTIME_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
}

run_candidate_python_runtime_smoke() {
  local root="${1:?}"
  local py="${2:-}" smoke timeout command rc

  if (( ${BOT_CHANGED:-0} == 0 )); then
    PREFLIGHT_RUNTIME_STATUS="não necessário"
    return 0
  fi

  if [[ -z "$py" ]]; then
    py="$(current_bot_python_bin)"
  fi
  if [[ -z "$py" || ! -x "$py" ]]; then
    PREFLIGHT_RUNTIME_STATUS="falhou: Python indisponível"
    LAST_ERROR_STDERR="$PREFLIGHT_RUNTIME_STATUS"
    LAST_ERROR_CODE="BOT_RUNTIME_SMOKE_PYTHON_MISSING"
    return 1
  fi

  smoke="$root/updater/utilitarios/verificacao_runtime.py"
  if [[ ! -f "$smoke" ]]; then
    PREFLIGHT_RUNTIME_STATUS="falhou: updater/utilitarios/verificacao_runtime.py ausente"
    LAST_ERROR_STDERR="$PREFLIGHT_RUNTIME_STATUS"
    LAST_ERROR_CODE="BOT_RUNTIME_SMOKE_SCRIPT_MISSING"
    return 1
  fi

  timeout="${UPDATE_RUNTIME_SMOKE_TIMEOUT_SECONDS:-45}"
  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=45
  (( timeout < 5 )) && timeout=5
  (( timeout > 180 )) && timeout=180

  STAGE="smoke runtime Python do candidato"
  printf -v command 'cd %q && timeout %qs env PYTHONDONTWRITEBYTECODE=1 UPDATE_RUNTIME_SMOKE=1 PYTHONPATH=%q %q %q --root %q' \
    "$root" "$timeout" "$root" "$py" "$smoke" "$root"

  if zip_progress_run_as_ubuntu \
    "Validando runtime Python" \
    "Importando bot, cogs e comandos sem conectar ao Discord" \
    "$command"
  then
    if (( ${REQUIREMENTS_CHANGED:-0} == 1 )); then
      PREFLIGHT_RUNTIME_STATUS="OK no ambiente Python candidato"
    else
      PREFLIGHT_RUNTIME_STATUS="OK"
    fi
    return 0
  else
    rc=$?
    register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-$command}"
    if (( rc == 124 )); then
      LAST_ERROR_CODE="BOT_RUNTIME_SMOKE_TIMEOUT"
      PREFLIGHT_RUNTIME_STATUS="falhou: timeout (${timeout}s)"
    else
      LAST_ERROR_CODE="BOT_RUNTIME_SMOKE_FAILED"
      PREFLIGHT_RUNTIME_STATUS="falhou"
    fi
    [[ -n "${LAST_ERROR_STDERR//[[:space:]]/}" ]] || LAST_ERROR_STDERR="$PREFLIGHT_RUNTIME_STATUS"
    return "$rc"
  fi
}

load_node_toolchain_identity() {
  if [[ -n "${NODE_TOOLCHAIN_NODE_VERSION:-}" && -n "${NODE_TOOLCHAIN_NPM_VERSION:-}" && -n "${NODE_TOOLCHAIN_PLATFORM:-}" ]]; then
    return 0
  fi
  local output node_version npm_version
  if ! output="$(sudo -u ubuntu -H bash -lc 'printf "%s\n%s\n" "$(node --version)" "$(npm --version)"' 2>/dev/null)"; then
    return 1
  fi
  node_version="${output%%$'\n'*}"
  npm_version="${output#*$'\n'}"
  [[ -n "$node_version" && -n "$npm_version" ]] || return 1
  NODE_TOOLCHAIN_NODE_VERSION="$node_version"
  NODE_TOOLCHAIN_NPM_VERSION="$npm_version"
  NODE_TOOLCHAIN_PLATFORM="$(uname -s)-$(uname -m)"
}

node_dependency_cache_key() {
  local project_dir="${1:?}" mode="${2:?}"
  [[ -s "$project_dir/package.json" && -s "$project_dir/package-lock.json" ]] || return 2
  # Harnesses antigos extraem o bloco a partir desta função. Mantenha um
  # fallback autocontido; no updater completo o helper já existe e o custo é 0.
  if ! declare -F load_node_toolchain_identity >/dev/null 2>&1; then
    load_node_toolchain_identity() {
      if [[ -n "${NODE_TOOLCHAIN_NODE_VERSION:-}" && -n "${NODE_TOOLCHAIN_NPM_VERSION:-}" && -n "${NODE_TOOLCHAIN_PLATFORM:-}" ]]; then
        return 0
      fi
      local output node_version npm_version
      output="$(sudo -u ubuntu -H bash -lc 'printf "%s\n%s\n" "$(node --version)" "$(npm --version)"' 2>/dev/null)" || return 1
      node_version="${output%%$'\n'*}"
      npm_version="${output#*$'\n'}"
      [[ -n "$node_version" && -n "$npm_version" ]] || return 1
      NODE_TOOLCHAIN_NODE_VERSION="$node_version"
      NODE_TOOLCHAIN_NPM_VERSION="$npm_version"
      NODE_TOOLCHAIN_PLATFORM="$(uname -s)-$(uname -m)"
    }
  fi
  load_node_toolchain_identity || return 1

  local package_hash lock_hash
  package_hash="$(sha256sum "$project_dir/package.json" | awk '{print $1}')"
  lock_hash="$(sha256sum "$project_dir/package-lock.json" | awk '{print $1}')"
  printf '%s\0%s\0%s\0%s\0%s\0' \
    "$mode" "$NODE_TOOLCHAIN_NODE_VERSION" "$NODE_TOOLCHAIN_NPM_VERSION" "$NODE_TOOLCHAIN_PLATFORM" "$package_hash:$lock_hash" \
    | sha256sum | awk '{print $1}'
}

node_dependency_layer_root() {
  local kind="${1:?}" mode="${2:?}" key="${3:?}" cache_root
  cache_root="${NODE_DEPENDENCY_CACHE_ROOT:-${CANDIDATE_ROOT:-${TMPDIR:-/tmp}/tts-bot-node-dependency-cache}}"
  [[ "$kind" =~ ^[a-z0-9_-]+$ && "$mode" =~ ^[a-z0-9_-]+$ && "$key" =~ ^[a-f0-9]{64}$ ]] || return 1
  printf '%s/%s/%s/%s\n' "$cache_root" "$kind" "$mode" "$key"
}

verify_node_dependency_layer() {
  local layer="${1:?}" expected_key="${2:?}" expected_mode="${3:?}"
  [[ -d "$layer" && ! -L "$layer" && -d "$layer/node_modules" && ! -L "$layer/node_modules" && -s "$layer/layer.json" && ! -L "$layer/layer.json" ]] || return 1
  NODE_LAYER_META="$layer/layer.json" NODE_LAYER_KEY="$expected_key" NODE_LAYER_MODE="$expected_mode" python3 - <<'PYNODELAYERVERIFY' >/dev/null 2>&1
import json, os, pathlib
path = pathlib.Path(os.environ['NODE_LAYER_META'])
data = json.loads(path.read_text(encoding='utf-8'))
if data.get('state') != 'ready':
    raise SystemExit(1)
if data.get('key') != os.environ['NODE_LAYER_KEY']:
    raise SystemExit(1)
if data.get('mode') != os.environ['NODE_LAYER_MODE']:
    raise SystemExit(1)
PYNODELAYERVERIFY
}

write_node_dependency_layer_manifest() {
  local layer="${1:?}" key="${2:?}" kind="${3:?}" mode="${4:?}" project_dir="${5:?}"
  if declare -F load_node_toolchain_identity >/dev/null 2>&1; then
    load_node_toolchain_identity || return 1
  else
    local output
    output="$(sudo -u ubuntu -H bash -lc 'printf "%s\n%s\n" "$(node --version)" "$(npm --version)"' 2>/dev/null)" || return 1
    NODE_TOOLCHAIN_NODE_VERSION="${output%%$'\n'*}"
    NODE_TOOLCHAIN_NPM_VERSION="${output#*$'\n'}"
    NODE_TOOLCHAIN_PLATFORM="$(uname -s)-$(uname -m)"
  fi
  local package_hash lock_hash
  package_hash="$(sha256sum "$project_dir/package.json" | awk '{print $1}')"
  lock_hash="$(sha256sum "$project_dir/package-lock.json" | awk '{print $1}')"
  NODE_LAYER_META="$layer/layer.json" NODE_LAYER_KEY="$key" NODE_LAYER_KIND="$kind" NODE_LAYER_MODE="$mode" \
  NODE_LAYER_NODE="$NODE_TOOLCHAIN_NODE_VERSION" NODE_LAYER_NPM="$NODE_TOOLCHAIN_NPM_VERSION" NODE_LAYER_PACKAGE_HASH="$package_hash" \
  NODE_LAYER_LOCK_HASH="$lock_hash" NODE_LAYER_PLATFORM="$NODE_TOOLCHAIN_PLATFORM" python3 - <<'PYNODELAYERWRITE'
import datetime, json, os, pathlib
path = pathlib.Path(os.environ['NODE_LAYER_META'])
payload = {
    'state': 'ready',
    'key': os.environ['NODE_LAYER_KEY'],
    'kind': os.environ['NODE_LAYER_KIND'],
    'mode': os.environ['NODE_LAYER_MODE'],
    'node': os.environ['NODE_LAYER_NODE'],
    'npm': os.environ['NODE_LAYER_NPM'],
    'package_sha256': os.environ['NODE_LAYER_PACKAGE_HASH'],
    'lock_sha256': os.environ['NODE_LAYER_LOCK_HASH'],
    'platform': os.environ['NODE_LAYER_PLATFORM'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
tmp = path.with_name('.layer.json.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYNODELAYERWRITE
}

attach_node_dependency_layer() {
  local project_dir="${1:?}" layer="${2:?}"
  [[ -d "$project_dir" && -d "$layer/node_modules" ]] || return 1
  rm -rf -- "$project_dir/node_modules" 2>/dev/null || return 1
  sudo -u ubuntu -H ln -s -- "$layer/node_modules" "$project_dir/node_modules"
}

prepare_node_dependency_layer() {
  local project_dir="${1:?}" kind="${2:?}" mode="${3:?}"
  local progress_title="${4:-Instalando dependências}" progress_detail="${5:-Preparando dependências}" key layer parent tmp qdir install_cmd
  LAST_NODE_DEP_LAYER_PATH=""
  LAST_NODE_DEP_LAYER_KEY=""
  LAST_NODE_DEP_CACHE_HIT=0

  [[ -d "$project_dir" ]] || return 1
  if [[ ! -s "$project_dir/package-lock.json" ]]; then
    return 2
  fi
  if declare -F load_node_toolchain_identity >/dev/null 2>&1; then load_node_toolchain_identity || return 1; fi
  key="$(node_dependency_cache_key "$project_dir" "$mode")" || return 1
  layer="$(node_dependency_layer_root "$kind" "$mode" "$key")" || return 1
  parent="$(dirname "$layer")"

  if verify_node_dependency_layer "$layer" "$key" "$mode"; then
    LAST_NODE_DEP_LAYER_PATH="$layer"
    LAST_NODE_DEP_LAYER_KEY="$key"
    LAST_NODE_DEP_CACHE_HIT=1
    NODE_DEP_CACHE_HITS=$(( ${NODE_DEP_CACHE_HITS:-0} + 1 ))
    touch "$layer/layer.json" 2>/dev/null || true
    if [[ "$mode" == "dev" ]]; then
      attach_node_dependency_layer "$project_dir" "$layer" || return 1
    fi
    logger -t "$LOG_TAG" "cache Node HIT: $kind/$mode ${key:0:12}" 2>/dev/null || true
    return 0
  fi

  NODE_DEP_CACHE_MISSES=$(( ${NODE_DEP_CACHE_MISSES:-0} + 1 ))
  rm -rf -- "$layer" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$parent" || return 1
  tmp="$(mktemp -d "$parent/.${key}.XXXXXX")" || return 1
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  chmod 0775 "$tmp" 2>/dev/null || true

  rm -rf -- "$project_dir/node_modules" 2>/dev/null || true
  printf -v qdir '%q' "$project_dir"
  local npm_flags="${NPM_INSTALL_FLAGS:---prefer-offline --no-audit --no-fund --progress=false}"
  install_cmd="cd $qdir && npm ci $npm_flags"
  if [[ "$mode" == "prod" ]]; then
    install_cmd="cd $qdir && npm ci --omit=dev $npm_flags"
  fi
  zip_progress_run_as_ubuntu "$progress_title" "$progress_detail · cache miss" "$install_cmd" || {
    local rc=$?
    rm -rf -- "$tmp" 2>/dev/null || true
    return "$rc"
  }
  if [[ ! -d "$project_dir/node_modules" || -L "$project_dir/node_modules" ]]; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! sudo -u ubuntu -H mv -- "$project_dir/node_modules" "$tmp/node_modules"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! write_node_dependency_layer_manifest "$tmp" "$key" "$kind" "$mode" "$project_dir"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  chown -R root:root "$tmp" 2>/dev/null || true
  chmod -R a-w "$tmp" 2>/dev/null || true

  if [[ -e "$layer" ]]; then
    rm -rf -- "$tmp" 2>/dev/null || true
  elif ! mv -- "$tmp" "$layer"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  verify_node_dependency_layer "$layer" "$key" "$mode" || return 1
  LAST_NODE_DEP_LAYER_PATH="$layer"
  LAST_NODE_DEP_LAYER_KEY="$key"
  if [[ "$mode" == "dev" ]]; then
    attach_node_dependency_layer "$project_dir" "$layer" || return 1
  fi
  logger -t "$LOG_TAG" "cache Node MISS preparado: $kind/$mode ${key:0:12}" 2>/dev/null || true
  return 0
}


backend_live_dependency_layer() {
  local project_dir="${1:-${BACK_DIR:-}}" modules
  local target layer key parent tmp backup
  modules="$project_dir/node_modules"
  LAST_NODE_DEP_LAYER_PATH=""
  LAST_NODE_DEP_LAYER_KEY=""
  LAST_NODE_DEP_CACHE_HIT=0

  [[ -d "$project_dir" && -s "$project_dir/package.json" && -s "$project_dir/package-lock.json" ]] || return 1

  if [[ -L "$modules" ]]; then
    target="$(readlink -f -- "$modules" 2>/dev/null || true)"
    [[ -n "$target" && "$(basename "$target")" == "node_modules" ]] || return 1
    layer="$(dirname "$target")"
    key="$(basename "$layer")"
    [[ "$key" =~ ^[a-f0-9]{64}$ ]] || return 1
    verify_node_dependency_layer "$layer" "$key" prod || return 1
    LAST_NODE_DEP_LAYER_PATH="$layer"
    LAST_NODE_DEP_LAYER_KEY="$key"
    LAST_NODE_DEP_CACHE_HIT=1
    touch "$layer/layer.json" 2>/dev/null || true
    return 0
  fi

  [[ -d "$modules" ]] || return 1
  if declare -F load_node_toolchain_identity >/dev/null 2>&1; then load_node_toolchain_identity || return 1; fi
  key="$(node_dependency_cache_key "$project_dir" prod)" || return 1
  layer="$(node_dependency_layer_root backend prod "$key")" || return 1
  parent="$(dirname "$layer")"

  if verify_node_dependency_layer "$layer" "$key" prod; then
    backup="$project_dir/.previous-node-modules-adopt-${UPDATE_RUNTIME_RUN_ID//[^[:alnum:]._-]/_}"
    rm -rf -- "$backup" 2>/dev/null || true
    sudo -u ubuntu -H mv -- "$modules" "$backup" || return 1
    if ! sudo -u ubuntu -H ln -s -- "$layer/node_modules" "$modules"; then
      sudo -u ubuntu -H mv -- "$backup" "$modules" 2>/dev/null || true
      return 1
    fi
    rm -rf -- "$backup" 2>/dev/null || true
    LAST_NODE_DEP_LAYER_PATH="$layer"
    LAST_NODE_DEP_LAYER_KEY="$key"
    LAST_NODE_DEP_CACHE_HIT=1
    touch "$layer/layer.json" 2>/dev/null || true
    logger -t "$LOG_TAG" "backend live migrou para dependency layer existente ${key:0:12}" 2>/dev/null || true
    return 0
  fi

  install -d -o ubuntu -g ubuntu -m 0775 "$parent" || return 1
  tmp="$(mktemp -d "$parent/.adopt-${key}.XXXXXX")" || return 1
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  chmod 0775 "$tmp" 2>/dev/null || true

  if ! sudo -u ubuntu -H mv -- "$modules" "$tmp/node_modules"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! write_node_dependency_layer_manifest "$tmp" "$key" backend prod "$project_dir"; then
    sudo -u ubuntu -H mv -- "$tmp/node_modules" "$modules" 2>/dev/null || true
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  chown -R root:root "$tmp" 2>/dev/null || true
  chmod -R a-w "$tmp" 2>/dev/null || true

  if [[ -e "$layer" ]]; then
    chmod -R u+w "$tmp" 2>/dev/null || true
    sudo -u ubuntu -H mv -- "$tmp/node_modules" "$modules" 2>/dev/null || true
    rm -rf -- "$tmp" 2>/dev/null || true
    verify_node_dependency_layer "$layer" "$key" prod || return 1
  elif ! mv -- "$tmp" "$layer"; then
    chmod -R u+w "$tmp" 2>/dev/null || true
    sudo -u ubuntu -H mv -- "$tmp/node_modules" "$modules" 2>/dev/null || true
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi

  if ! sudo -u ubuntu -H ln -s -- "$layer/node_modules" "$modules"; then
    # Último recurso de segurança: restaure os mesmos arquivos ao path live.
    chmod -R u+w "$layer" 2>/dev/null || true
    mv -- "$layer/node_modules" "$modules" 2>/dev/null || true
    chown -R ubuntu:ubuntu "$modules" 2>/dev/null || true
    rm -rf -- "$layer" 2>/dev/null || true
    return 1
  fi

  LAST_NODE_DEP_LAYER_PATH="$layer"
  LAST_NODE_DEP_LAYER_KEY="$key"
  logger -t "$LOG_TAG" "backend live adotado como dependency layer ${key:0:12} sem cópia" 2>/dev/null || true
  return 0
}

collect_protected_node_dependency_keys() {
  local target key file
  if [[ -n "${BACK_DIR:-}" && -L "${BACK_DIR}/node_modules" ]]; then
    target="$(readlink -f -- "${BACK_DIR}/node_modules" 2>/dev/null || true)"
    if [[ -n "$target" && "$(basename "$target")" == "node_modules" ]]; then
      key="$(basename "$(dirname "$target")")"
      [[ "$key" =~ ^[a-f0-9]{64}$ ]] && printf '%s\n' "$key"
    fi
  fi

  while IFS= read -r file; do
    [[ -s "$file" ]] || continue
    NODE_REF_MANIFEST="$file" python3 - <<'PYNODEREFS' 2>/dev/null || true
import json, os
try:
    with open(os.environ['NODE_REF_MANIFEST'], encoding='utf-8') as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)
record = data.get('backend') if isinstance(data, dict) else None
if isinstance(record, dict):
    key = str(record.get('deps_key') or '')
    if len(key) == 64:
        print(key)
PYNODEREFS
  done < <(
    {
      [[ -n "${RUNTIME_RELEASE_ROOT:-}" ]] && find "$RUNTIME_RELEASE_ROOT" -mindepth 2 -maxdepth 2 -type f -name release.json -print 2>/dev/null || true
      [[ -n "${CANDIDATE_ROOT:-}" ]] && find "$CANDIDATE_ROOT" -mindepth 4 -maxdepth 4 -type f -name ready.json -path '*/runtime-artifacts/*/ready.json' -print 2>/dev/null || true
      [[ -n "${REMOTE_RUNTIME_ARTIFACT_ROOT:-}" ]] && find "$REMOTE_RUNTIME_ARTIFACT_ROOT" -mindepth 2 -maxdepth 2 -type f -name ready.json -print 2>/dev/null || true
    } | sort -u
  )
}

prune_node_dependency_layers() {
  local keep="${NODE_DEPENDENCY_CACHE_RETENTION:-4}" group count entry cache_root key
  cache_root="${NODE_DEPENDENCY_CACHE_ROOT:-${CANDIDATE_ROOT:-${TMPDIR:-/tmp}/tts-bot-node-dependency-cache}}"
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=4
  (( keep >= 1 )) || keep=1
  [[ -d "$cache_root" ]] || return 0

  declare -A protected=()
  while IFS= read -r key; do
    [[ "$key" =~ ^[a-f0-9]{64}$ ]] && protected["$key"]=1
  done < <(collect_protected_node_dependency_keys | sort -u)

  while IFS= read -r group; do
    [[ -d "$group" ]] || continue
    count=0
    while IFS= read -r entry; do
      [[ -d "$entry" ]] || continue
      key="$(basename "$entry")"
      if [[ -n "${protected[$key]:-}" ]]; then
        continue
      fi
      count=$((count + 1))
      if (( count > keep )); then
        rm -rf -- "$entry" 2>/dev/null || true
      fi
    done < <(find "$group" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
  done < <(find "$cache_root" -mindepth 2 -maxdepth 2 -type d -print 2>/dev/null)
}

frontend_typescript_cache_key() {
  local project_dir="${1:?}" dep_key tsconfig_hash
  [[ -s "$project_dir/tsconfig.json" && -s "$project_dir/package.json" && -s "$project_dir/package-lock.json" ]] || return 2
  if declare -F load_node_toolchain_identity >/dev/null 2>&1; then load_node_toolchain_identity || return 1; fi
  dep_key="$(node_dependency_cache_key "$project_dir" dev)" || return 1
  tsconfig_hash="$(sha256sum "$project_dir/tsconfig.json" | awk '{print $1}')"
  printf '%s\0%s\0%s\0' "frontend-noemit-v1" "$dep_key" "$tsconfig_hash" \
    | sha256sum | awk '{print $1}'
}

frontend_typescript_cache_entry() {
  local key="${1:?}" root
  root="${TYPESCRIPT_CACHE_ROOT:-${CANDIDATE_ROOT:-${TMPDIR:-/tmp}/tts-bot-typescript-cache}}"
  [[ "$key" =~ ^[a-f0-9]{64}$ ]] || return 1
  printf '%s/frontend/%s\n' "$root" "$key"
}

verify_frontend_typescript_cache() {
  local entry="${1:?}" expected_key="${2:?}"
  [[ -d "$entry" && ! -L "$entry" && -s "$entry/tsconfig.tsbuildinfo" && ! -L "$entry/tsconfig.tsbuildinfo" && -s "$entry/cache.json" && ! -L "$entry/cache.json" ]] || return 1
  TYPESCRIPT_CACHE_META="$entry/cache.json" TYPESCRIPT_CACHE_KEY="$expected_key" python3 - <<'PYTSCACHEVERIFY' >/dev/null 2>&1
import json, os, pathlib
path = pathlib.Path(os.environ['TYPESCRIPT_CACHE_META'])
data = json.loads(path.read_text(encoding='utf-8'))
if data.get('state') != 'ready' or data.get('kind') != 'frontend-noemit':
    raise SystemExit(1)
if data.get('key') != os.environ['TYPESCRIPT_CACHE_KEY']:
    raise SystemExit(1)
PYTSCACHEVERIFY
}

write_frontend_typescript_cache_manifest() {
  local entry="${1:?}" key="${2:?}" project_dir="${3:?}" tsconfig_hash
  tsconfig_hash="$(sha256sum "$project_dir/tsconfig.json" | awk '{print $1}')"
  TYPESCRIPT_CACHE_META="$entry/cache.json" TYPESCRIPT_CACHE_KEY="$key" TYPESCRIPT_TSCONFIG_HASH="$tsconfig_hash" python3 - <<'PYTSCACHEWRITE'
import datetime, json, os, pathlib
path = pathlib.Path(os.environ['TYPESCRIPT_CACHE_META'])
payload = {
    'state': 'ready',
    'kind': 'frontend-noemit',
    'key': os.environ['TYPESCRIPT_CACHE_KEY'],
    'tsconfig_sha256': os.environ['TYPESCRIPT_TSCONFIG_HASH'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
tmp = path.with_name('.cache.json.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYTSCACHEWRITE
}

run_frontend_incremental_typecheck() {
  local project_dir="${1:?}" key entry parent tmp local_cache qdir
  LAST_TYPESCRIPT_CACHE_HIT=0
  [[ -x "$project_dir/node_modules/.bin/tsc" && -s "$project_dir/tsconfig.json" ]] || return 1
  key="$(frontend_typescript_cache_key "$project_dir")" || return 1
  entry="$(frontend_typescript_cache_entry "$key")" || return 1
  parent="$(dirname "$entry")"
  local_cache="$project_dir/.update-tscache"
  rm -rf -- "$local_cache" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$local_cache" || return 1

  if verify_frontend_typescript_cache "$entry" "$key"; then
    # A camada compartilhada é imutável; a cópia local precisa aceitar escrita do tsc.
    sudo -u ubuntu -H install -m 0644 -- "$entry/tsconfig.tsbuildinfo" "$local_cache/tsconfig.tsbuildinfo" || return 1
    LAST_TYPESCRIPT_CACHE_HIT=1
    TYPESCRIPT_CACHE_HITS=$(( ${TYPESCRIPT_CACHE_HITS:-0} + 1 ))
    touch "$entry/cache.json" 2>/dev/null || true
    logger -t "$LOG_TAG" "cache TypeScript HIT: frontend ${key:0:12}" 2>/dev/null || true
  else
    TYPESCRIPT_CACHE_MISSES=$(( ${TYPESCRIPT_CACHE_MISSES:-0} + 1 ))
    logger -t "$LOG_TAG" "cache TypeScript MISS: frontend ${key:0:12}" 2>/dev/null || true
  fi

  printf -v qdir '%q' "$project_dir"
  zip_progress_run_as_ubuntu \
    "Validando TypeScript" \
    "Typecheck incremental do frontend" \
    "cd $qdir && ./node_modules/.bin/tsc -p tsconfig.json --incremental --tsBuildInfoFile .update-tscache/tsconfig.tsbuildinfo --noEmit" || return $?

  # Cache é uma otimização: se uma versão futura do TypeScript deixar de gerar
  # buildinfo com --noEmit, o typecheck continua válido e só perdemos o HIT.
  if [[ ! -s "$local_cache/tsconfig.tsbuildinfo" || -L "$local_cache/tsconfig.tsbuildinfo" ]]; then
    rm -rf -- "$local_cache" 2>/dev/null || true
    return 0
  fi

  install -d -o ubuntu -g ubuntu -m 0775 "$parent" || return 1
  tmp="$(mktemp -d "$parent/.${key}.XXXXXX")" || return 1
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  if ! sudo -u ubuntu -H cp -a -- "$local_cache/tsconfig.tsbuildinfo" "$tmp/tsconfig.tsbuildinfo"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  if ! write_frontend_typescript_cache_manifest "$tmp" "$key" "$project_dir"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  chown -R root:root "$tmp" 2>/dev/null || true
  chmod -R a-w "$tmp" 2>/dev/null || true
  if [[ -e "$entry" ]]; then
    chmod -R u+w "$entry" 2>/dev/null || true
    rm -rf -- "$entry" 2>/dev/null || true
  fi
  if ! mv -- "$tmp" "$entry"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  rm -rf -- "$local_cache" 2>/dev/null || true
  return 0
}

backend_typescript_cache_key() {
  local project_dir="${1:?}" dep_key tsconfig_hash
  [[ -s "$project_dir/tsconfig.json" && -s "$project_dir/package.json" && -s "$project_dir/package-lock.json" ]] || return 2
  if declare -F load_node_toolchain_identity >/dev/null 2>&1; then load_node_toolchain_identity || return 1; fi
  dep_key="$(node_dependency_cache_key "$project_dir" dev)" || return 1
  tsconfig_hash="$(sha256sum "$project_dir/tsconfig.json" | awk '{print $1}')"
  printf '%s\0%s\0%s\0' "backend-emit-v1" "$dep_key" "$tsconfig_hash" \
    | sha256sum | awk '{print $1}'
}

backend_typescript_cache_entry() {
  local key="${1:?}" root
  root="${TYPESCRIPT_CACHE_ROOT:-${CANDIDATE_ROOT:-${TMPDIR:-/tmp}/tts-bot-typescript-cache}}"
  [[ "$key" =~ ^[a-f0-9]{64}$ ]] || return 1
  printf '%s/backend/%s\n' "$root" "$key"
}

backend_typescript_dist_hash() {
  local dist_dir="${1:?}"
  [[ -d "$dist_dir" && ! -L "$dist_dir" ]] || return 1
  TYPESCRIPT_DIST_DIR="$dist_dir" python3 - <<'PYBACKTSDISTHASH'
import hashlib, os, pathlib
root = pathlib.Path(os.environ['TYPESCRIPT_DIST_DIR'])
h = hashlib.sha256()
for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        raise SystemExit(2)
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit(3)
    h.update(rel.encode('utf-8'))
    h.update(b'\0')
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    h.update(b'\0')
print(h.hexdigest())
PYBACKTSDISTHASH
}

verify_backend_typescript_cache() {
  local entry="${1:?}" expected_key="${2:?}" expected_hash actual_hash
  [[ -d "$entry" && ! -L "$entry" && -s "$entry/tsconfig.tsbuildinfo" && ! -L "$entry/tsconfig.tsbuildinfo" \
     && -s "$entry/cache.json" && ! -L "$entry/cache.json" && -s "$entry/dist/index.js" && ! -L "$entry/dist" ]] || return 1
  expected_hash="$(TYPESCRIPT_CACHE_META="$entry/cache.json" TYPESCRIPT_CACHE_KEY="$expected_key" python3 - <<'PYBACKTSCACHEVERIFY' 2>/dev/null
import json, os, pathlib
path = pathlib.Path(os.environ['TYPESCRIPT_CACHE_META'])
data = json.loads(path.read_text(encoding='utf-8'))
if data.get('state') != 'ready' or data.get('kind') != 'backend-emit':
    raise SystemExit(1)
if data.get('key') != os.environ['TYPESCRIPT_CACHE_KEY']:
    raise SystemExit(1)
value = str(data.get('dist_sha256') or '')
if len(value) != 64:
    raise SystemExit(1)
print(value)
PYBACKTSCACHEVERIFY
)" || return 1
  actual_hash="$(backend_typescript_dist_hash "$entry/dist")" || return 1
  [[ "$actual_hash" == "$expected_hash" ]]
}

write_backend_typescript_cache_manifest() {
  local entry="${1:?}" key="${2:?}" project_dir="${3:?}" dist_hash="${4:?}" tsconfig_hash
  tsconfig_hash="$(sha256sum "$project_dir/tsconfig.json" | awk '{print $1}')"
  TYPESCRIPT_CACHE_META="$entry/cache.json" TYPESCRIPT_CACHE_KEY="$key" TYPESCRIPT_TSCONFIG_HASH="$tsconfig_hash" TYPESCRIPT_DIST_HASH="$dist_hash" python3 - <<'PYBACKTSCACHEWRITE'
import datetime, json, os, pathlib
path = pathlib.Path(os.environ['TYPESCRIPT_CACHE_META'])
payload = {
    'state': 'ready',
    'kind': 'backend-emit',
    'key': os.environ['TYPESCRIPT_CACHE_KEY'],
    'tsconfig_sha256': os.environ['TYPESCRIPT_TSCONFIG_HASH'],
    'dist_sha256': os.environ['TYPESCRIPT_DIST_HASH'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
tmp = path.with_name('.cache.json.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYBACKTSCACHEWRITE
}

backend_typescript_cache_can_seed() {
  # TypeScript não remove automaticamente JS órfão quando um source some.
  # Nesse caso, force build limpo para que dist não retenha um módulo deletado.
  if printf '%s\n' "${CHANGED_STATUS_RAW:-}" | grep -Eq '^D[[:space:]]+dashboard/backend/src/.*[.]ts$'; then
    return 1
  fi
  return 0
}

run_backend_incremental_build() {
  local project_dir="${1:?}" key entry parent tmp local_cache qdir dist_hash
  LAST_BACKEND_TYPESCRIPT_CACHE_HIT=0
  [[ -x "$project_dir/node_modules/.bin/tsc" && -s "$project_dir/tsconfig.json" ]] || return 1
  key="$(backend_typescript_cache_key "$project_dir")" || return 1
  entry="$(backend_typescript_cache_entry "$key")" || return 1
  parent="$(dirname "$entry")"
  local_cache="$project_dir/.update-tscache-backend"
  rm -rf -- "$local_cache" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$local_cache" || return 1

  if backend_typescript_cache_can_seed && verify_backend_typescript_cache "$entry" "$key"; then
    # A camada compartilhada é imutável; a cópia local precisa aceitar escrita do tsc.
    sudo -u ubuntu -H install -m 0644 -- "$entry/tsconfig.tsbuildinfo" "$local_cache/tsconfig.tsbuildinfo" || return 1
    rm -rf -- "$project_dir/dist" 2>/dev/null || true
    if ! sudo -u ubuntu -H cp -a --reflink=auto --no-preserve=ownership -- "$entry/dist" "$project_dir/dist" 2>/dev/null; then
      sudo -u ubuntu -H cp -a --no-preserve=ownership -- "$entry/dist" "$project_dir/dist" || return 1
    fi
    chmod -R u+w "$project_dir/dist" 2>/dev/null || true
    LAST_BACKEND_TYPESCRIPT_CACHE_HIT=1
    TYPESCRIPT_CACHE_HITS=$(( ${TYPESCRIPT_CACHE_HITS:-0} + 1 ))
    touch "$entry/cache.json" 2>/dev/null || true
    logger -t "$LOG_TAG" "cache TypeScript HIT: backend ${key:0:12}" 2>/dev/null || true
  else
    rm -rf -- "$project_dir/dist" 2>/dev/null || true
    TYPESCRIPT_CACHE_MISSES=$(( ${TYPESCRIPT_CACHE_MISSES:-0} + 1 ))
    logger -t "$LOG_TAG" "cache TypeScript MISS: backend ${key:0:12}" 2>/dev/null || true
  fi

  printf -v qdir '%q' "$project_dir"
  zip_progress_run_as_ubuntu \
    "Compilando servidor" \
    "TypeScript incremental do backend" \
    "cd $qdir && ./node_modules/.bin/tsc -p tsconfig.json --incremental --tsBuildInfoFile .update-tscache-backend/tsconfig.tsbuildinfo && node scripts/copy-command-catalog.mjs" || return $?

  if [[ ! -s "$project_dir/dist/index.js" ]]; then
    rm -rf -- "$local_cache" 2>/dev/null || true
    return 1
  fi
  # Cache é só otimização. Se o compilador não gerar buildinfo, o artefato já
  # está correto; apenas não haverá HIT na próxima execução.
  if [[ ! -s "$local_cache/tsconfig.tsbuildinfo" || -L "$local_cache/tsconfig.tsbuildinfo" ]]; then
    rm -rf -- "$local_cache" 2>/dev/null || true
    return 0
  fi

  dist_hash="$(backend_typescript_dist_hash "$project_dir/dist")" || {
    rm -rf -- "$local_cache" 2>/dev/null || true
    return 1
  }
  install -d -o ubuntu -g ubuntu -m 0775 "$parent" || return 1
  tmp="$(mktemp -d "$parent/.${key}.XXXXXX")" || return 1
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  if ! sudo -u ubuntu -H cp -- "$local_cache/tsconfig.tsbuildinfo" "$tmp/tsconfig.tsbuildinfo"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  if ! sudo -u ubuntu -H cp -a -- "$project_dir/dist" "$tmp/dist"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  if ! write_backend_typescript_cache_manifest "$tmp" "$key" "$project_dir" "$dist_hash"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  chown -R root:root "$tmp" 2>/dev/null || true
  chmod -R a-w "$tmp" 2>/dev/null || true
  if [[ -e "$entry" ]]; then
    chmod -R u+w "$entry" 2>/dev/null || true
    rm -rf -- "$entry" 2>/dev/null || true
  fi
  if ! mv -- "$tmp" "$entry"; then
    rm -rf -- "$tmp" "$local_cache" 2>/dev/null || true
    return 1
  fi
  rm -rf -- "$local_cache" 2>/dev/null || true
  return 0
}

prune_typescript_cache() {
  local keep="${TYPESCRIPT_CACHE_RETENTION:-4}" root group count entry
  root="${TYPESCRIPT_CACHE_ROOT:-${CANDIDATE_ROOT:-${TMPDIR:-/tmp}/tts-bot-typescript-cache}}"
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=4
  (( keep >= 1 )) || keep=1
  [[ -d "$root" ]] || return 0
  while IFS= read -r group; do
    [[ -d "$group" ]] || continue
    count=0
    while IFS= read -r entry; do
      [[ -d "$entry" ]] || continue
      count=$((count + 1))
      if (( count > keep )); then
        rm -rf -- "$entry" 2>/dev/null || true
      fi
    done < <(find "$group" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null)
}

select_node_test_plan() {
  local project_dir="${1:?}" prefix="${2:?}"
  local repo_root="${REPO_DIR:-$(pwd)}"
  local selector="${UPDATE_TEST_SELECTOR_SCRIPT:-$repo_root/updater/utilitarios/selecao_testes.py}"
  local -a plan_lines=()
  local meta mode selected total reason
  SELECTED_NODE_TEST_COMMAND="npm test"
  SELECTED_NODE_TEST_STATUS="suíte completa (fallback)"

  if [[ -z "${CHANGED_FILES_RAW:-}" ]]; then
    logger -t "$LOG_TAG" "diff indisponível para seleção de testes em $prefix; usando suíte completa" 2>/dev/null || true
    return 0
  fi
  if [[ ! -f "$selector" ]]; then
    logger -t "$LOG_TAG" "seletor de testes ausente em $selector; usando suíte completa" 2>/dev/null || true
    return 0
  fi
  if ! mapfile -t plan_lines < <(
    CHANGED_FILES_RAW_INPUT="${CHANGED_FILES_RAW:-}" python3 "$selector" \
      --project "$project_dir" --prefix "$prefix" --format shell
  ); then
    logger -t "$LOG_TAG" "seletor de testes falhou para $prefix; usando suíte completa" 2>/dev/null || true
    return 0
  fi
  if (( ${#plan_lines[@]} < 2 )); then
    logger -t "$LOG_TAG" "seletor de testes retornou plano incompleto para $prefix; usando suíte completa" 2>/dev/null || true
    return 0
  fi

  meta="${plan_lines[0]}"
  IFS=$'\t' read -r mode selected total reason <<< "$meta"
  [[ "$selected" =~ ^[0-9]+$ ]] || selected=0
  [[ "$total" =~ ^[0-9]+$ ]] || total=0
  case "$mode" in
    selected)
      SELECTED_NODE_TEST_COMMAND="${plan_lines[1]}"
      SELECTED_NODE_TEST_STATUS="${selected}/${total} selecionado(s) por impacto"
      ;;
    none)
      SELECTED_NODE_TEST_COMMAND=":"
      SELECTED_NODE_TEST_STATUS="0/${total}; nenhum teste necessário"
      ;;
    *)
      SELECTED_NODE_TEST_COMMAND="npm test"
      SELECTED_NODE_TEST_STATUS="suíte completa ${total}/${total} (${reason:-fallback})"
      ;;
  esac
  logger -t "$LOG_TAG" "plano de testes $prefix: $SELECTED_NODE_TEST_STATUS" 2>/dev/null || true
}

bot_health_profile_for_changed_files() {
  # Perfis só reduzem a janela quando o próprio diff prova que o impacto é
  # restrito. Variáveis UPDATE_BOT_HEALTH_* continuam podendo sobrescrever.
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(bot\.py|config\.py|db\.py|start\.sh|requirements\.txt|deploy/systemd(/vps)?/tts-bot\.service$)'; then
    printf 'critical'
    return 0
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(cogs/musica/|utility/)'; then
    printf 'standard'
    return 0
  fi
  if [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] && ! printf '%s\n' "$CHANGED_FILES_RAW" \
      | grep -Ev '^(tests/|docs/)' \
      | grep -Ev '^cogs/' \
      | grep -q .; then
    printf 'cogs'
    return 0
  fi
  printf 'standard'
}

reuse_ready_artifacts_for_commit() {
  local artifact_commit="${1:?}" root reusable_ready=1
  root="$(local_candidate_artifact_root_for_commit "$artifact_commit")" || return 1
  [[ -s "$root/ready.json" && ! -L "$root/ready.json" ]] || return 1

  if ! hydrate_local_candidate_runtime_artifacts "$artifact_commit"; then
    return 1
  fi
  if (( FRONT_CHANGED == 1 )) && ! verify_local_candidate_artifact_integrity frontend; then
    reusable_ready=0
  fi
  if (( BACK_CHANGED == 1 )) && ! verify_local_candidate_artifact_integrity backend; then
    reusable_ready=0
  fi
  if (( ${REQUIREMENTS_CHANGED:-0} == 1 )) && ! verify_local_candidate_artifact_integrity python; then
    reusable_ready=0
  fi
  if (( reusable_ready != 1 )); then
    LOCAL_CANDIDATE_RUNTIME_READY=0
    LOCAL_CANDIDATE_ARTIFACT_ROOT=""
    LOCAL_CANDIDATE_ARTIFACT_COMMIT=""
    LOCAL_CANDIDATE_FRONTEND_ARTIFACT=""
    LOCAL_CANDIDATE_BACKEND_ARTIFACT=""
    LOCAL_CANDIDATE_BACKEND_DEP_KEY=""
    LOCAL_CANDIDATE_BACKEND_DEP_LAYER=""
    LOCAL_CANDIDATE_PYTHON_ARTIFACT=""
    LOCAL_CANDIDATE_PYTHON_READY=0
    return 1
  fi

  if (( FRONT_CHANGED == 1 )); then
    FRONT_STATUS="frontend READY reutilizado sem rebuild"
    FRONT_TEST_PLAN_STATUS="READY reutilizado; testes não repetidos"
  elif (( ${FRONT_TESTS_CHANGED:-0} == 1 )); then
    FRONT_STATUS="validação frontend READY reutilizada; runtime não alterado"
    FRONT_TEST_PLAN_STATUS="READY reutilizado; testes não repetidos"
  fi
  if (( BACK_CHANGED == 1 )); then
    BACK_STATUS="backend READY reutilizado sem rebuild"
    BACK_TEST_PLAN_STATUS="READY reutilizado; testes não repetidos"
  elif (( ${BACK_TESTS_CHANGED:-0} == 1 )); then
    BACK_STATUS="validação backend READY reutilizada; runtime não alterado"
    BACK_TEST_PLAN_STATUS="READY reutilizado; testes não repetidos"
  fi
  if (( ${BOT_CHANGED:-0} == 1 )); then
    PREFLIGHT_RUNTIME_STATUS="OK; READY reutilizado"
  fi
  READY_FAST_PATH_USED=1
  return 0
}

local_candidate_ready_commit_from_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -s "${LOCAL_CANDIDATE_DIR:-}/state.json" && ! -L "${LOCAL_CANDIDATE_DIR:-}/state.json" ]] || return 1
  CANDIDATE_STATE_FILE="$LOCAL_CANDIDATE_DIR/state.json" python3 - <<'PYLOCALREADYSTATE' 2>/dev/null
import json, os
try:
    with open(os.environ['CANDIDATE_STATE_FILE'], encoding='utf-8') as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(1)
if str(data.get('state') or '') != 'ready':
    raise SystemExit(1)
commit = str(data.get('commit') or '').strip().lower()
if not (40 <= len(commit) <= 64 and all(ch in '0123456789abcdef' for ch in commit)):
    raise SystemExit(1)
print(commit)
PYLOCALREADYSTATE
}

resume_local_ready_before_worktree() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  local prepared parent body
  prepared="$(local_candidate_ready_commit_from_state 2>/dev/null || true)"
  [[ -n "$prepared" ]] || return 1
  repo_git cat-file -e "${prepared}^{commit}" >/dev/null 2>&1 || return 1
  parent="$(repo_git rev-parse "${prepared}^" 2>/dev/null || true)"
  [[ -n "$parent" && "$parent" == "$CURRENT_COMMIT" ]] || return 1
  body="$(repo_git log -1 --pretty=%B "$prepared" 2>/dev/null || true)"
  printf '%s\n' "$body" | grep -Fqx "Candidate-ID: $LOCAL_CANDIDATE_ID" || return 1

  if ! load_git_diff_snapshot "$REPO_DIR" --base "$CURRENT_COMMIT" --target "$prepared"; then
    return 1
  fi
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 1
  classify_changed_files

  LOCAL_CANDIDATE_PREPARED_COMMIT="$prepared"
  if ! reuse_ready_artifacts_for_commit "$prepared"; then
    LOCAL_CANDIDATE_PREPARED_COMMIT=""
    return 1
  fi
  write_local_candidate_state "ready" "$prepared"
  logger -t "$LOG_TAG" "candidato ${LOCAL_CANDIDATE_ID:-desconhecido} retomou READY diretamente de $(short_commit "$prepared") sem recriar worktree" 2>/dev/null || true
  return 0
}

prepare_local_candidate_runtime_artifacts_in_worktree() {
  (( LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )) || return 0

  local validation_worktree artifact_commit candidate_label
  local npm_flags="${NPM_INSTALL_FLAGS:---prefer-offline --no-audit --no-fund --progress=false}"
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]] || return 1
    [[ -n "${LOCAL_CANDIDATE_PREPARED_COMMIT:-}" ]] || return 1
    validation_worktree="$LOCAL_CANDIDATE_WORKTREE_DIR"
    artifact_commit="$LOCAL_CANDIDATE_PREPARED_COMMIT"
    candidate_label="${LOCAL_CANDIDATE_ID:-candidato local}"
  else
    [[ -n "${REMOTE_WORKTREE_DIR:-}" && -d "$REMOTE_WORKTREE_DIR" ]] || return 1
    [[ -n "${REMOTE_COMMIT:-}" ]] || return 1
    validation_worktree="$REMOTE_WORKTREE_DIR"
    artifact_commit="$REMOTE_COMMIT"
    candidate_label="commit remoto $(short_commit "$REMOTE_COMMIT")"
  fi

  local root front_dir back_dir front_artifact back_artifact front_ready=0 back_ready=0 python_ready=0 smoke_py=""
  root="$(local_candidate_artifact_root_for_commit "$artifact_commit")" || return 1
  if [[ "${REMOTE_CANDIDATE_MODE:-0}" == "1" ]]; then
    # Registre o path antes de qualquer npm/cópia: se a validação falhar no
    # meio, o trap EXIT consegue remover também artefatos remotos parciais.
    REMOTE_CANDIDATE_ARTIFACT_ROOT="$root"
  fi
  # Mantemos estes dois assignments explícitos para facilitar auditoria e
  # regressões do caminho local; o remoto usa o worktree equivalente abaixo.
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    front_dir="$LOCAL_CANDIDATE_WORKTREE_DIR/dashboard/frontend"
    back_dir="$LOCAL_CANDIDATE_WORKTREE_DIR/dashboard/backend"
  else
    front_dir="$REMOTE_WORKTREE_DIR/dashboard/frontend"
    back_dir="$REMOTE_WORKTREE_DIR/dashboard/backend"
  fi
  front_artifact="$root/frontend/dist"
  back_artifact="$root/backend"

  # Test-only normalmente não publica nada. Se o runtime live já estiver
  # quebrado, transforme a validação em reparo antes de decidir se um READY
  # antigo ainda satisfaz este candidato.
  if (( FRONT_CHANGED == 0 && ${FRONT_TESTS_CHANGED:-0} == 1 )) && ! frontend_publication_is_healthy; then
    FRONT_CHANGED=1
    logger -t "$LOG_TAG" "frontend test-only encontrou publicação inválida; artefato de reparo será preparado no worktree" 2>/dev/null || true
  fi

  # READY é imutável e endereçado pelo commit. Em retry/resume, valide os
  # hashes e reutilize-o em vez de repetir npm/test/build/smoke.
  if reuse_ready_artifacts_for_commit "$artifact_commit"; then
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      write_local_candidate_state "ready" "$artifact_commit"
    fi
    logger -t "$LOG_TAG" "$candidate_label reutilizou READY validado de $(short_commit "$artifact_commit")" 2>/dev/null || true
    return 0
  fi

  rm -rf -- "$root" 2>/dev/null || true
  install -d -o ubuntu -g ubuntu -m 0775 "$root" || {
    LAST_ERROR_STDERR="não foi possível criar diretório persistente de artefatos: $root"
    LAST_ERROR_CODE="CANDIDATE_ARTIFACT_DIR_FAILED"
    return 1
  }

  # Dependências Python alteradas são preparadas em um runtime versionado e
  # persistente antes do smoke. A .venv live não é tocada durante staging.
  if (( ${REQUIREMENTS_CHANGED:-0} == 1 )); then
    prepare_candidate_python_runtime "$validation_worktree" "$artifact_commit" || return $?
    smoke_py="$LOCAL_CANDIDATE_PYTHON_ARTIFACT/venv/bin/python"
    python_ready=1
  fi

  # O smoke Python roda antes de npm ci/build para falhar cedo. Ele é comum ao
  # caminho ZIP e ao caminho remoto porque ambos chegam aqui ainda no worktree.
  run_candidate_python_runtime_smoke "$validation_worktree" "$smoke_py" || return $?

  if (( FRONT_CHANGED == 1 || ${FRONT_TESTS_CHANGED:-0} == 1 )); then
    [[ -d "$front_dir" ]] || {
      LAST_ERROR_STDERR="frontend não encontrado no worktree: $front_dir"
      LAST_ERROR_CODE="FRONTEND_SOURCE_MISSING"
      return 1
    }
    STAGE="dependências do frontend"
    if [[ -s "$front_dir/package-lock.json" ]]; then
      prepare_node_dependency_layer "$front_dir" frontend dev \
        "Preparando dependências" "Validando frontend no worktree" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-npm ci $npm_flags}"
          return "$rc"
        }
    else
      zip_progress_run_as_ubuntu \
        "Instalando dependências" \
        "Validando frontend no worktree · sem lockfile" \
        "cd \"$front_dir\" && npm install $npm_flags" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-npm install $npm_flags}"
          return "$rc"
        }
    fi

    if (( ${FRONT_TESTS_REQUIRED:-0} == 1 )); then
      STAGE="testes do frontend"
      select_node_test_plan "$front_dir" "dashboard/frontend"
      FRONT_TEST_PLAN_STATUS="$SELECTED_NODE_TEST_STATUS"
      local front_test_command="${SELECTED_NODE_TEST_COMMAND:-npm test}"
      zip_progress_run_as_ubuntu \
        "Validando interface" \
        "Executando testes no worktree · $FRONT_TEST_PLAN_STATUS" \
        "cd \"$front_dir\" && $front_test_command" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-$front_test_command}"
          return "$rc"
        }
    fi

    if (( FRONT_CHANGED == 1 )); then
      if (( ${FRONT_TYPECHECK_REQUIRED:-0} == 1 )); then
        STAGE="typecheck do frontend"
        run_frontend_incremental_typecheck "$front_dir" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-tsc incremental}"
          return "$rc"
        }
      fi

      STAGE="build do frontend"
      zip_progress_run_as_ubuntu \
        "Compilando interface" \
        "Gerando bundle Vite isolado" \
        "cd \"$front_dir\" && ./node_modules/.bin/vite build && node scripts/normalize-dist-permissions.mjs" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-vite build}"
          return "$rc"
        }

      if [[ ! -s "$front_dir/dist/index.html" ]]; then
        FRONT_STATUS="build isolado do frontend não produziu dist/index.html"
        LAST_ERROR_STDERR="$FRONT_STATUS"
        LAST_ERROR_CODE="FRONTEND_BUILD_FAILED"
        return 1
      fi
      install -d -o ubuntu -g ubuntu -m 0775 "$(dirname "$front_artifact")"
      sudo -u ubuntu -H cp -a -- "$front_dir/dist" "$front_artifact"
      front_ready=1
      if (( ${FRONT_TYPECHECK_REQUIRED:-0} == 1 )); then
        if (( ${LAST_TYPESCRIPT_CACHE_HIT:-0} == 1 )); then
          FRONT_STATUS="frontend validado; typecheck incremental com cache HIT e bundle Vite gerado"
        else
          FRONT_STATUS="frontend validado; typecheck incremental e bundle Vite gerado"
        fi
      elif (( ${FRONT_TESTS_REQUIRED:-0} == 0 )); then
        FRONT_STATUS="frontend visual recompilado sem testes/typecheck desnecessários"
      else
        FRONT_STATUS="frontend validado e bundle Vite gerado"
      fi
    else
      FRONT_STATUS="testes do frontend aprovados; runtime não alterado"
    fi
  fi

  if (( BACK_CHANGED == 1 || ${BACK_TESTS_CHANGED:-0} == 1 )); then
    [[ -d "$back_dir" ]] || {
      LAST_ERROR_STDERR="backend não encontrado no worktree: $back_dir"
      LAST_ERROR_CODE="BACKEND_SOURCE_MISSING"
      return 1
    }
    STAGE="dependências do backend"
    if [[ -s "$back_dir/package-lock.json" ]]; then
      prepare_node_dependency_layer "$back_dir" backend dev \
        "Preparando servidor" "Validando backend no worktree" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-npm ci $npm_flags}"
          return "$rc"
        }
    else
      zip_progress_run_as_ubuntu \
        "Preparando servidor" \
        "Validando backend no worktree · sem lockfile" \
        "cd \"$back_dir\" && npm install $npm_flags" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-npm install $npm_flags}"
          return "$rc"
        }
    fi

    if (( ${BACK_TESTS_REQUIRED:-1} == 1 )); then
      STAGE="testes do backend"
      select_node_test_plan "$back_dir" "dashboard/backend"
      BACK_TEST_PLAN_STATUS="$SELECTED_NODE_TEST_STATUS"
      local back_test_command="${SELECTED_NODE_TEST_COMMAND:-npm test}"
      zip_progress_run_as_ubuntu \
        "Validando servidor" \
        "Executando testes no worktree · $BACK_TEST_PLAN_STATUS" \
        "cd \"$back_dir\" && $back_test_command" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-$back_test_command}"
          return "$rc"
        }
    fi

    if (( BACK_CHANGED == 1 )); then
      STAGE="build do backend"
      run_backend_incremental_build "$back_dir" || {
        local rc=$?
        register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-tsc incremental backend}"
        return "$rc"
      }

      if [[ ! -s "$back_dir/dist/index.js" ]]; then
        BACK_STATUS="build isolado do backend não produziu dist/index.js"
        LAST_ERROR_STDERR="$BACK_STATUS"
        LAST_ERROR_CODE="BACKEND_BUILD_FAILED"
        return 1
      fi

      local backend_dep_key="" backend_dep_layer=""
      STAGE="dependências do backend (produção)"
      if [[ ! -s "$back_dir/package-lock.json" ]]; then
        BACK_STATUS="backend sem package-lock.json; runtime imutável exige lockfile"
        LAST_ERROR_STDERR="$BACK_STATUS"
        LAST_ERROR_CODE="BACKEND_LOCKFILE_REQUIRED"
        return 1
      fi
      prepare_node_dependency_layer "$back_dir" backend prod \
        "Preparando runtime do servidor" "Dependências de produção · cache" || {
          local rc=$?
          register_error_context "$rc" "${CURRENT_STAGE_COMMAND:-npm ci --omit=dev $npm_flags}"
          return "$rc"
        }
      backend_dep_key="$LAST_NODE_DEP_LAYER_KEY"
      backend_dep_layer="$LAST_NODE_DEP_LAYER_PATH"
      if [[ ! "$backend_dep_key" =~ ^[a-f0-9]{64}$ ]] || ! verify_node_dependency_layer "$backend_dep_layer" "$backend_dep_key" prod; then
        BACK_STATUS="runtime isolado do backend não produziu dependency layer válido"
        LAST_ERROR_STDERR="$BACK_STATUS"
        LAST_ERROR_CODE="BACKEND_DEPENDENCY_LAYER_INVALID"
        return 1
      fi

      install -d -o ubuntu -g ubuntu -m 0775 "$back_artifact"
      sudo -u ubuntu -H cp -a -- "$back_dir/dist" "$back_artifact/dist"
      BACK_ARTIFACT_DEPS_FILE="$back_artifact/deps.json" BACK_ARTIFACT_DEPS_KEY="$backend_dep_key" python3 - <<'PYBACKARTIFACTDEPS'
import json, os, pathlib
path = pathlib.Path(os.environ['BACK_ARTIFACT_DEPS_FILE'])
payload = {'mode': 'prod', 'deps_key': os.environ['BACK_ARTIFACT_DEPS_KEY']}
tmp = path.with_name('.deps.json.tmp')
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYBACKARTIFACTDEPS
      chown ubuntu:ubuntu "$back_artifact/deps.json" 2>/dev/null || true
      back_ready=1
      if (( ${LAST_BACKEND_TYPESCRIPT_CACHE_HIT:-0} == 1 )); then
        BACK_STATUS="backend validado e compilado incrementalmente com cache HIT"
      elif (( ${BACK_TESTS_REQUIRED:-1} == 0 )); then
        BACK_STATUS="backend recompilado incrementalmente sem testes desnecessários"
      else
        BACK_STATUS="backend validado e compilado incrementalmente no worktree"
      fi
    else
      BACK_STATUS="testes do backend aprovados; runtime não alterado"
    fi
  fi

  # Validação/build não pode alterar nenhum arquivo rastreado do commit.
  # dist/node_modules são artefatos gerados; se uma ferramenta modificar source
  # ou lockfile rastreado, o candidato deixa de ser reproduzível e é rejeitado.
  local tracked_mutations
  tracked_mutations="$(worktree_git "$validation_worktree" status --porcelain=v1 --untracked-files=no 2>/dev/null || true)"
  if [[ -n "${tracked_mutations//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="validação alterou arquivos rastreados no worktree:
$tracked_mutations"
    LAST_ERROR_CODE="VALIDATOR_MUTATED_SOURCE_TREE"
    return 1
  fi

  if ! write_local_candidate_artifact_ready_manifest "$root" "$artifact_commit" "$front_ready" "$back_ready" "$python_ready"; then
    LAST_ERROR_STDERR="não foi possível registrar manifesto dos artefatos preparados"
    LAST_ERROR_CODE="CANDIDATE_ARTIFACT_MANIFEST_FAILED"
    return 1
  fi
  # frontend/backend artifacts are created as ubuntu already. Avoid a recursive
  # chown/chmod over dist trees on every update; only ready.json is emitted by
  # the privileged coordinator and needs normalization here.
  chown ubuntu:ubuntu "$root/ready.json" 2>/dev/null || true
  chmod 0644 "$root/ready.json" 2>/dev/null || true

  if ! hydrate_local_candidate_runtime_artifacts "$artifact_commit"; then
    LAST_ERROR_STDERR="artefatos preparados não passaram pela hidratação de READY"
    LAST_ERROR_CODE="CANDIDATE_ARTIFACT_READY_INVALID"
    return 1
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "ready" "$artifact_commit"
  fi
  logger -t "$LOG_TAG" "$candidate_label atingiu READY com artefatos isolados em $root" 2>/dev/null || true
  return 0
}

promote_local_candidate_worktree_commit() {
  [[ -n "${LOCAL_CANDIDATE_PREPARED_COMMIT:-}" ]] || {
    LAST_ERROR_STDERR="commit isolado do candidato não foi preparado"
    LAST_ERROR_CODE="CANDIDATE_WORKTREE_COMMIT_MISSING"
    return 1
  }

  local live_head dirty
  live_head="$(repo_git rev-parse HEAD)"
  if [[ "$live_head" != "$CURRENT_COMMIT" ]]; then
    LAST_ERROR_STDERR="HEAD live mudou durante a validação isolada: esperado $(short_commit "$CURRENT_COMMIT"), atual $(short_commit "$live_head")"
    LAST_ERROR_CODE="LIVE_HEAD_CHANGED_BEFORE_PROMOTION"
    return 1
  fi
  dirty="$(collect_local_tracked_changes || true)"
  if [[ -n "${dirty//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="checkout live ficou sujo antes da promoção do candidato:
$dirty"
    LAST_ERROR_CODE="DIRTY_LIVE_BEFORE_PROMOTION"
    return 1
  fi

  STAGE="promoção atômica do candidato"
  if ! repo_git merge --ff-only "$LOCAL_CANDIDATE_PREPARED_COMMIT" >/dev/null; then
    LAST_ERROR_STDERR="não foi possível promover por fast-forward o commit isolado do candidato"
    LAST_ERROR_CODE="CANDIDATE_PROMOTION_FAILED"
    return 1
  fi

  UPDATE_APPLIED=1
  LOCAL_CANDIDATE_ALREADY_PROMOTED=1
  REMOTE_COMMIT="$LOCAL_CANDIDATE_PREPARED_COMMIT"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  write_local_candidate_state "promoted" "$REMOTE_COMMIT"
  discard_local_candidate_worktree || true
  logger -t "$LOG_TAG" "candidato ${LOCAL_CANDIDATE_ID:-desconhecido} promovido ao checkout live: $(short_commit "$REMOTE_COMMIT")" 2>/dev/null || true
  return 0
}

local_live_head_candidate_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  local live_head origin_head body parent
  live_head="$(repo_git rev-parse HEAD 2>/dev/null || true)"
  origin_head="${REMOTE_COMMIT:-}"
  [[ -n "$live_head" ]] || return 1
  body="$(repo_git log -1 --pretty=%B "$live_head" 2>/dev/null || true)"
  printf '%s\n' "$body" | grep -Fqx "Candidate-ID: $LOCAL_CANDIDATE_ID" || return 1

  parent="$(repo_git rev-parse "${live_head}^" 2>/dev/null || true)"
  if [[ -n "$origin_head" && "$live_head" == "$origin_head" ]]; then
    # O commit já chegou ao GitHub em execução anterior; só falta a entrega final.
    PREVIOUS_COMMIT="${parent:-$live_head}"
    CURRENT_COMMIT="$PREVIOUS_COMMIT"
    LOCAL_CANDIDATE_PREPARED_COMMIT="$live_head"
    LOCAL_CANDIDATE_PUBLISHED=1
    LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=1
    REMOTE_COMMIT="$live_head"
    SHORT_FROM="$(short_commit "$PREVIOUS_COMMIT")"
    SHORT_TO="$(short_commit "$live_head")"
    mark_deployment_committed
    hydrate_local_candidate_runtime_artifacts "$live_head" || true
    return 0
  fi

  if [[ -n "$origin_head" && -n "$parent" && "$parent" == "$origin_head" ]]; then
    # A execução anterior promoveu o commit local, mas caiu antes do push.
    PREVIOUS_COMMIT="$parent"
    CURRENT_COMMIT="$parent"
    LOCAL_CANDIDATE_PREPARED_COMMIT="$live_head"
    LOCAL_CANDIDATE_ALREADY_PROMOTED=1
    UPDATE_APPLIED=1
    REMOTE_COMMIT="$live_head"
    SHORT_FROM="$(short_commit "$parent")"
    SHORT_TO="$(short_commit "$live_head")"
    hydrate_local_candidate_runtime_artifacts "$live_head" || true
    if declare -F load_git_diff_snapshot >/dev/null 2>&1; then
      if ! load_git_diff_snapshot "$REPO_DIR" --base "$parent" --target "$live_head"; then
        return 1
      fi
    else
      CHANGED_STATUS_RAW="$(repo_git diff --name-status --no-renames "$parent" "$live_head")"
      CHANGED_FILES_RAW="$(repo_git diff --name-only --no-renames "$parent" "$live_head")"
      CHANGED_DIFF_NUMSTAT_RAW="$(repo_git diff --numstat --no-renames "$parent" "$live_head")"
    fi
    classify_changed_files
    write_local_candidate_state "promoted" "$live_head"
    return 0
  fi
  return 1
}

refresh_changed_files_from_staged_diff() {
  # Uma única chamada Git produz status, paths e numstat. A intenção do
  # manifesto deixa de ser autoridade após o stage; este snapshot é a fonte
  # única usada por classificação, testes e relatório.
  local root
  if declare -F candidate_repo_dir >/dev/null 2>&1; then
    root="$(candidate_repo_dir)"
  else
    root="${REPO_DIR:-.}"
  fi
  if declare -F load_git_diff_snapshot >/dev/null 2>&1; then
    if ! load_git_diff_snapshot "$root" --cached; then
      LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-falha ao calcular snapshot do diff staged do candidato}"
      return 1
    fi
    return 0
  fi
  # Compatibilidade com harnesses/execuções parciais do helper.
  if ! CHANGED_STATUS_RAW="$(candidate_git diff --cached --name-status --no-renames)"; then return 1; fi
  if ! CHANGED_FILES_RAW="$(candidate_git diff --cached --name-only --no-renames)"; then return 1; fi
  if ! CHANGED_DIFF_NUMSTAT_RAW="$(candidate_git diff --cached --numstat --no-renames)"; then return 1; fi
  return 0
}

apply_local_candidate_patch_diff() {
  [[ -f "${LOCAL_CANDIDATE_PATCH_FILE:-}" ]] || return 1
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-apply.XXXXXX")"
  if candidate_git apply --3way --index "$LOCAL_CANDIDATE_PATCH_FILE" 2>"$errfile"; then
    rm -f "$errfile" 2>/dev/null || true
    return 0
  fi
  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true
  return 1
}

apply_local_candidate_operations() {
  (( LOCAL_CANDIDATE_SCHEMA_VERSION >= 3 )) || return 0
  local manifest="$LOCAL_CANDIDATE_DIR/manifest.json"
  local op first second apply_repo
  apply_repo="$(candidate_repo_dir)"

  while IFS=$'\t' read -r op first second; do
    [[ -n "$op" ]] || continue
    case "$op" in
      delete)
        if [[ -L "$apply_repo/$first" ]]; then
          LAST_ERROR_STDERR="delete não aceita symlink: $first"
          LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
          return 1
        fi
        if [[ -f "$apply_repo/$first" ]]; then
          if ! candidate_git ls-files --error-unmatch -- "$first" >/dev/null 2>&1; then
            LAST_ERROR_STDERR="delete exige arquivo rastreado pelo Git: $first"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
            return 1
          fi
          if ! candidate_git rm -f -- "$first"; then
            LAST_ERROR_STDERR="git rm falhou para operação delete: $first"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_APPLY_FAILED"
            return 1
          fi
        elif candidate_git diff --cached --name-only --no-renames --diff-filter=D -- "$first" | grep -Fqx -- "$first"; then
          : # retomada: deleção já staged
        elif candidate_git ls-files --error-unmatch -- "$first" >/dev/null 2>&1; then
          # Retomada após remoção do worktree, antes do stage.
          if ! candidate_git add -A -- "$first"; then
            LAST_ERROR_STDERR="não foi possível retomar delete pendente: $first"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_APPLY_FAILED"
            return 1
          fi
        else
          LAST_ERROR_STDERR="delete não encontrou arquivo rastreado nem deleção staged: $first"
          LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
          return 1
        fi
        ;;
      move)
        if [[ -L "$apply_repo/$first" || -L "$apply_repo/$second" ]]; then
          LAST_ERROR_STDERR="move não aceita symlink: $first -> $second"
          LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
          return 1
        fi
        if [[ -f "$apply_repo/$first" && ! -e "$apply_repo/$second" ]]; then
          if ! candidate_git ls-files --error-unmatch -- "$first" >/dev/null 2>&1; then
            LAST_ERROR_STDERR="move exige origem rastreada pelo Git: $first"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
            return 1
          fi
          sudo -u ubuntu -H mkdir -p -- "$apply_repo/$(dirname "$second")"
          if ! candidate_git mv -- "$first" "$second"; then
            LAST_ERROR_STDERR="git mv falhou: $first -> $second"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_APPLY_FAILED"
            return 1
          fi
        elif [[ ! -e "$apply_repo/$first" && -f "$apply_repo/$second" ]]; then
          if candidate_git diff --cached --name-only --no-renames --diff-filter=D -- "$first" | grep -Fqx -- "$first" \
            && candidate_git diff --cached --name-only --no-renames --diff-filter=A -- "$second" | grep -Fqx -- "$second"; then
            : # retomada: git mv já staged
          elif candidate_git ls-files --error-unmatch -- "$first" >/dev/null 2>&1; then
            local source_blob target_blob
            source_blob="$(candidate_git rev-parse "HEAD:$first" 2>/dev/null || true)"
            target_blob="$(candidate_git hash-object "$apply_repo/$second" 2>/dev/null || true)"
            if [[ -z "$source_blob" || "$source_blob" != "$target_blob" ]]; then
              LAST_ERROR_STDERR="move parcial não corresponde ao blob original: $first -> $second"
              LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
              return 1
            fi
            candidate_git add -A -- "$first" "$second" || {
              LAST_ERROR_STDERR="não foi possível retomar move pendente: $first -> $second"
              LAST_ERROR_CODE="CANDIDATE_OPERATION_APPLY_FAILED"
              return 1
            }
          else
            LAST_ERROR_STDERR="move não corresponde a estado staged/rastreado: $first -> $second"
            LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
            return 1
          fi
        elif [[ -e "$apply_repo/$first" && -e "$apply_repo/$second" ]]; then
          LAST_ERROR_STDERR="destino de move já existe: $second"
          LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
          return 1
        else
          LAST_ERROR_STDERR="move não encontrou origem nem destino válido: $first -> $second"
          LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
          return 1
        fi
        ;;
      add|update)
        # Conteúdo é copiado em copy_local_candidate_files().
        ;;
      *)
        LAST_ERROR_STDERR="operação declarativa desconhecida no manifesto: $op"
        LAST_ERROR_CODE="CANDIDATE_OPERATION_INVALID"
        return 1
        ;;
    esac
  done < <(python3 - "$manifest" <<'PYOPS'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
for item in data.get('operations') or []:
    if not isinstance(item, dict):
        continue
    op = str(item.get('op') or '').strip().lower()
    if op == 'move':
        print(f"move\t{item.get('from', '')}\t{item.get('to', '')}")
    else:
        print(f"{op}\t{item.get('path', '')}\t")
PYOPS
)
  return 0
}

copy_local_candidate_files() {
  [[ -d "$LOCAL_CANDIDATE_FILES_DIR" ]] || return 1
  sudo -u ubuntu -H env MANIFEST_PATH="$LOCAL_CANDIDATE_DIR/manifest.json" APPLY_REPO_DIR="$(candidate_repo_dir)" FILES_DIR="$LOCAL_CANDIDATE_FILES_DIR" python3 - <<'PYCOPY'
import json, os, pathlib, shutil
repo = pathlib.Path(os.environ['APPLY_REPO_DIR']).resolve()
files_dir = pathlib.Path(os.environ['FILES_DIR']).resolve()
data = json.loads(pathlib.Path(os.environ['MANIFEST_PATH']).read_text(encoding='utf-8'))
try:
    schema_version = int(data.get('schema_version') or 2)
except (TypeError, ValueError):
    schema_version = 2
if schema_version >= 3:
    raw_paths = [
        item.get('path')
        for item in (data.get('operations') or [])
        if isinstance(item, dict) and str(item.get('op') or '').strip().lower() in {'add', 'update'}
    ]
else:
    raw_paths = data.get('changed_files') or []
for raw in raw_paths:
    rel = pathlib.PurePosixPath(str(raw))
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido no candidato: {raw}')
    src = files_dir.joinpath(*rel.parts).resolve()
    dst = repo.joinpath(*rel.parts).resolve()
    if files_dir not in src.parents and src != files_dir:
        raise SystemExit(f'origem fora do candidato: {raw}')
    if repo not in dst.parents and dst != repo:
        raise SystemExit(f'destino fora do repo: {raw}')
    if not src.is_file():
        raise SystemExit(f'arquivo ausente no candidato: {raw}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
PYCOPY
}

normalize_changed_file_permissions() {
  local context="${1:-permissões do candidato}"
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$(candidate_repo_dir)" python3 - <<'PYPERM'
import os, pathlib, pwd, grp, stat
repo = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')).resolve()
raw_items = os.environ.get('CHANGED_FILES_RAW', '').splitlines()
try:
    uid = pwd.getpwnam('ubuntu').pw_uid
    gid = grp.getgrnam('ubuntu').gr_gid
except KeyError:
    raise SystemExit(0)

def inside_repo(path: pathlib.Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        return False
    return resolved == repo or repo in resolved.parents

def apply_owner_mode(path: pathlib.Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except Exception:
        pass
    try:
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISDIR(st.st_mode):
            os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        elif stat.S_ISREG(st.st_mode):
            os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

for raw in raw_items:
    raw = raw.strip()
    if not raw:
        continue
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido para permissões: {raw}')
    dst = repo.joinpath(*rel.parts)
    cur = repo
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            if not inside_repo(cur):
                raise SystemExit(f'parent fora do repo: {raw}')
            apply_owner_mode(cur)
    if dst.exists() or dst.is_symlink():
        if not inside_repo(dst):
            raise SystemExit(f'path fora do repo: {raw}')
        apply_owner_mode(dst)
PYPERM
  logger -t "$LOG_TAG" "permissões normalizadas para paths do candidato: $context" 2>/dev/null || true
}

prune_empty_candidate_dirs_after_reset() {
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYPRUNE'
import os, pathlib
repo = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')).resolve()
parents = []
for raw in os.environ.get('CHANGED_FILES_RAW', '').splitlines():
    raw = raw.strip()
    if not raw:
        continue
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or '..' in rel.parts:
        continue
    path = repo.joinpath(*rel.parts)
    parent = path.parent
    while parent != repo:
        try:
            resolved = parent.resolve(strict=False)
        except Exception:
            break
        if not (resolved == repo or repo in resolved.parents):
            break
        parents.append(parent)
        parent = parent.parent
for parent in sorted(set(parents), key=lambda p: len(p.parts), reverse=True):
    try:
        parent.rmdir()
    except OSError:
        pass
PYPRUNE
}

cleanup_local_candidate_new_files_after_reset() {
  # Antes da promoção, toda mutação do candidato vive exclusivamente no
  # worktree. Nunca remova paths do checkout live nessa fase: um arquivo novo
  # do candidato pode coincidir com um untracked local legítimo.
  if (( LOCAL_CANDIDATE_MODE == 0 || UPDATE_APPLIED == 0 )) || [[ -z "${PREVIOUS_COMMIT:-}" ]]; then
    return 0
  fi
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    if printf '%s
' "$rel" | grep -Eq '(^|/)\.\.(\/|$)|^/'; then
      continue
    fi
    if repo_git cat-file -e "$PREVIOUS_COMMIT:$rel" 2>/dev/null; then
      continue
    fi
    sudo -u ubuntu -H rm -rf -- "$REPO_DIR/$rel" 2>/dev/null || sudo rm -rf -- "$REPO_DIR/$rel" 2>/dev/null || true
  done <<< "$CHANGED_FILES_RAW"
  prune_empty_candidate_dirs_after_reset || true
}

git_add_changed_files() {
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0

  # Stage seguro para criação, alteração e remoção. Um `git add path` direto
  # pode falhar com "pathspec did not match" quando o path não existe no
  # worktree; `git add -A` stageia deleções, mas ainda falha para paths que
  # nunca foram rastreados. Por isso só passamos paths existentes/symlinks ou
  # paths que o Git já conhece.
  local pathspec_file rel target tracked_any rc apply_repo
  apply_repo="$(candidate_repo_dir)"
  pathspec_file="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-pathspec.XXXXXX")"
  tracked_any=0

  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    if printf '%s\n' "$rel" | grep -Eq '(^|/)\.\.(\/|$)|^/'; then
      rm -f "$pathspec_file" 2>/dev/null || true
      LAST_ERROR_STDERR="path inválido para git add: $rel"
      return 1
    fi

    target="$apply_repo/$rel"
    if [[ -e "$target" || -L "$target" ]]; then
      printf '%s\0' "$rel" >> "$pathspec_file"
      tracked_any=1
    elif candidate_git ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
      # Arquivo removido pelo patch: stageia a deleção com `git add -A`.
      printf '%s\0' "$rel" >> "$pathspec_file"
      tracked_any=1
    else
      logger -t "$LOG_TAG" "ignorando path ausente/não rastreado no stage: $rel" 2>/dev/null || true
    fi
  done <<< "$CHANGED_FILES_RAW"

  if (( tracked_any == 0 )) || [[ ! -s "$pathspec_file" ]]; then
    rm -f "$pathspec_file" 2>/dev/null || true
    return 0
  fi

  # mktemp roda no usuário do updater (normalmente root), enquanto o Git roda
  # como ubuntu. Torne o pathspec legível antes de entregá-lo ao git add.
  chown ubuntu:ubuntu "$pathspec_file" 2>/dev/null || true
  chmod 0644 "$pathspec_file" 2>/dev/null || true
  candidate_git add -A --pathspec-from-file="$pathspec_file" --pathspec-file-nul
  rc=$?
  rm -f "$pathspec_file" 2>/dev/null || true
  return "$rc"
}

prepare_local_candidate_update() {
  LOCAL_CANDIDATE_MODE=1
  # Git, worktree e staging são curtos e sensíveis à latência. Use prioridade
  # moderada só nesta janela; trabalhos pesados retornam ao perfil conservador.
  set_updater_priority_profile fast
  zip_progress_publish "Conferindo ZIP" "Checando arquivo recebido e base local."
  STAGE="fetch remoto"
  local fetch_started_ms
  fetch_started_ms="$(update_now_ms)"
  if ! fetch_remote_for_local_candidate; then
    log_update_operation_timing_ms "preflight.git_fetch" "$fetch_started_ms"
    return 1
  fi
  if (( REMOTE_FETCH_REUSED == 1 )); then
    log_update_operation_timing_ms "preflight.git_fetch_reuse" "$fetch_started_ms"
  else
    log_update_operation_timing_ms "preflight.git_fetch" "$fetch_started_ms"
  fi
  if ! load_repo_ref_snapshot "$BRANCH"; then return 1; fi
  REMOTE_COMMIT="$GIT_REFS_REMOTE"
  CURRENT_COMMIT="$GIT_REFS_CURRENT"
  if (( REMOTE_FETCH_REUSED == 0 )); then
    record_remote_fetch_state "$REMOTE_COMMIT" || true
  fi
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  COMMIT_SUBJECT="$LOCAL_CANDIDATE_COMMIT_MESSAGE"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="local"
  if (( REMOTE_FETCH_REUSED == 1 )); then
    mark_update_timing "fetch_reuse"
  else
    mark_update_timing "fetch"
  fi
  zip_progress_done_and_publish "Base conferida" "Validando integridade do pacote"

  local max_attempts="${DISCORD_AUTO_UPDATE_MAX_ATTEMPTS:-3}"
  [[ "$max_attempts" =~ ^[0-9]+$ ]] || max_attempts=3
  (( max_attempts < 1 )) && max_attempts=1
  if [[ "${LOCAL_CANDIDATE_ATTEMPT:-0}" =~ ^[0-9]+$ ]] && (( LOCAL_CANDIDATE_ATTEMPT > max_attempts )); then
    reject_local_candidate_safely       "Atualização arquivada"       "O pacote excedeu o limite de tentativas automáticas e não foi aplicado."       "tentativa ${LOCAL_CANDIDATE_ATTEMPT}/${max_attempts}; reenvie o patch após revisar a falha anterior"
  fi

  local max_resumes="${DISCORD_AUTO_UPDATE_MAX_RESUMES:-5}"
  [[ "$max_resumes" =~ ^[0-9]+$ ]] || max_resumes=5
  (( max_resumes < 1 )) && max_resumes=1
  if [[ "${LOCAL_CANDIDATE_RESUME_COUNT:-0}" =~ ^[0-9]+$ ]] && (( LOCAL_CANDIDATE_RESUME_COUNT > max_resumes )); then
    reject_local_candidate_safely \
      "Atualização arquivada" \
      "O candidato foi interrompido vezes demais e não será retomado automaticamente." \
      "retomadas ${LOCAL_CANDIDATE_RESUME_COUNT}/${max_resumes}; revise a causa das interrupções antes de reenviar"
  fi

  STAGE="validação de integridade do candidato"
  if ! verify_local_candidate_integrity; then
    reject_local_candidate_safely \
      "Atualização bloqueada" \
      "O pacote não passou pela verificação de integridade ou expirou. Nenhuma alteração foi aplicada." \
      "$LOCAL_CANDIDATE_VERIFY_ERROR"
  fi
  update_local_candidate_heartbeat "validated"
  zip_progress_done_and_publish "Integridade confirmada" "Analisando segurança"

  STAGE="validação de segurança do ZIP"
  local suspicion_reason=""
  suspicion_reason="$(local_candidate_suspicion_reason 2>/dev/null || true)"
  if [[ -n "${suspicion_reason//[[:space:]]/}" ]]; then
    reject_local_candidate_safely \
      "Atualização bloqueada" \
      "Esse arquivo parece uma base completa ou contém caminhos suspeitos. Nenhuma alteração foi aplicada." \
      "$suspicion_reason"
  fi
  zip_progress_done_and_publish "Segurança confirmada" "Validando permissões"

  STAGE="preflight de permissões do candidato"
  if ! preflight_local_candidate_permissions; then
    reject_local_candidate_safely \
      "Atualização bloqueada" \
      "Os arquivos do ZIP não podem ser aplicados com segurança pelo usuário do repositório. Nada foi aplicado." \
      "${LAST_ERROR_STDERR:-preflight de permissões falhou}"
  fi
  zip_progress_done_and_publish "Permissões confirmadas" "Validando estado local"

  # Se uma execução anterior caiu depois da promoção (ou até depois do push),
  # reconheça o commit pelo Candidate-ID antes de tentar sincronizar/aplicar de
  # novo. Isso torna a retomada segura sem depender de um worktree antigo.
  local git_preflight_started_ms prepare_started_ms
  git_preflight_started_ms="$(update_now_ms)"
  if local_live_head_candidate_state; then
    log_update_operation_timing_ms "preflight.git_head_candidate" "$git_preflight_started_ms"
    if (( LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY == 1 )); then
      logger -t "$LOG_TAG" "Candidato local já publicado; retomando apenas a entrega final." 2>/dev/null || true
    else
      logger -t "$LOG_TAG" "Candidato local já promovido; retomando validação/runtime antes do push." 2>/dev/null || true
    fi
    set_updater_priority_profile safe
    return 0
  fi
  log_update_operation_timing_ms "preflight.git_head_candidate" "$git_preflight_started_ms"

  if [[ -n "$LOCAL_CANDIDATE_BASE_COMMIT" && "$LOCAL_CANDIDATE_BASE_COMMIT" != "$REMOTE_COMMIT" ]]; then
    if [[ -f "${LOCAL_CANDIDATE_PATCH_FILE:-}" ]]; then
      LOCAL_CANDIDATE_USE_PATCH=1
      logger -t "$LOG_TAG" "Candidato $LOCAL_CANDIDATE_ID preparado sobre $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT"); tentará rebase 3-way sobre $(short_commit "$REMOTE_COMMIT")."
    else
      base_conflict_reason="$(local_candidate_base_conflict_reason 2>/dev/null || true)"
      if [[ -n "${base_conflict_reason//[[:space:]]/}" ]]; then
        MANUAL_FAILURE_ALERT_SENT=1
        notify_zip_status_message "error" "Atualização com conflito" "Esta atualização ficou incompatível com outra aplicada antes dela. Nada foi aplicado neste item; os próximos permanecem na fila." || true
        archive_local_candidate "failed"
        send_error "Update com conflito na fila" "Resumo: O ZIP foi preparado sobre outro commit e conflitou com mudanças já aplicadas. Nada foi aplicado e nada foi enviado ao GitHub para este item.
Branch: $BRANCH
Base do ZIP: $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT")
GitHub atual: $(short_commit "$REMOTE_COMMIT")
Motivo: $base_conflict_reason
Candidato: ${LOCAL_CANDIDATE_ID:-desconhecido}
ZIP: ${LOCAL_CANDIDATE_ZIP_NAME:-desconhecido}
Arquivos:
$(format_changed_files)
Ação sugerida: gere esse patch novamente usando a base atual.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
        trigger_updater_if_queue_pending
        exit 0
      fi
      logger -t "$LOG_TAG" "Candidato $LOCAL_CANDIDATE_ID preparado sobre $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT"), aplicando sobre $(short_commit "$REMOTE_COMMIT") sem conflito de arquivos."
    fi
  fi

  STAGE="verificação de alterações locais"
  git_preflight_started_ms="$(update_now_ms)"
  if ! load_repo_status_snapshot; then
    log_update_operation_timing_ms "preflight.git_status" "$git_preflight_started_ms"
    LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-não foi possível conferir as alterações locais do candidato como usuário ubuntu}"
    return 1
  fi
  log_update_operation_timing_ms "preflight.git_status" "$git_preflight_started_ms"

  # O mesmo snapshot alimenta a limpeza do marker e a validação do candidato.
  # Antes estes dois passos executavam `git status` separadamente, duplicando a
  # varredura do índice/worktree exatamente no caminho crítico de inicialização.
  clear_local_changes_marker_if_clean 1
  git_preflight_started_ms="$(update_now_ms)"
  if candidate_local_changes_are_expected 1; then
    log_update_operation_timing_ms "preflight.local_changes_check" "$git_preflight_started_ms"
    logger -t "$LOG_TAG" "Alterações locais correspondem ao candidato ativo; retomando aplicação segura."
  else
    local candidate_dirty_rc=$?
    log_update_operation_timing_ms "preflight.local_changes_check" "$git_preflight_started_ms"
    if (( candidate_dirty_rc == 2 )); then
      LAST_ERROR_STDERR="não foi possível conferir as alterações locais do candidato como usuário ubuntu"
      return 1
    fi
    fail_local_changes_before_pull
  fi

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" ]]; then
    STAGE="sincronização com GitHub antes do candidato"
    git_preflight_started_ms="$(update_now_ms)"
    repo_git pull --ff-only origin "$BRANCH"
    CURRENT_COMMIT="$(repo_git rev-parse HEAD)"
    PREVIOUS_COMMIT="$CURRENT_COMMIT"
    SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
    log_update_operation_timing_ms "preflight.git_pull" "$git_preflight_started_ms"
    mark_update_timing "sync"
  fi
  zip_progress_done_and_publish "Estado local validado" "Preparando arquivos"

  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    notify_zip_status_message "success" "Nenhuma alteração necessária" "O pacote já corresponde ao estado atual da VPS. Nenhum arquivo foi modificado." || true
    archive_local_candidate "done"
    logger -t "$LOG_TAG" "Candidato local sem arquivos alterados"
    trigger_updater_if_queue_pending
    exit 0
  fi

  classify_changed_files

  local local_ready_fast_path=0
  STAGE="retomada READY do candidato"
  if resume_local_ready_before_worktree; then
    local_ready_fast_path=1
    zip_progress_done_and_publish "READY anterior confirmado" "Preservando runtime atual"
  else
    STAGE="criação do worktree isolado"
    prepare_started_ms="$(update_now_ms)"
    if ! create_local_candidate_worktree; then
      log_update_operation_timing_ms "preparation.worktree_create" "$prepare_started_ms"
      reject_local_candidate_safely \
        "Atualização bloqueada" \
        "Não consegui criar a área isolada para testar este candidato. A árvore live não foi alterada." \
        "${LAST_ERROR_STDERR:-falha ao criar worktree isolado}"
    fi
    log_update_operation_timing_ms "preparation.worktree_create" "$prepare_started_ms"

    zip_progress_done_and_publish "ZIP conferido" "Aplicando em área isolada"

    STAGE="aplicação isolada do candidato"
    if (( LOCAL_CANDIDATE_USE_PATCH == 1 )); then
      prepare_started_ms="$(update_now_ms)"
      if ! apply_local_candidate_patch_diff; then
        log_update_operation_timing_ms "preparation.apply_patch" "$prepare_started_ms"
        LAST_ERROR_CODE="CANDIDATE_PATCH_CONFLICT"
        reject_local_candidate_safely \
          "Atualização com conflito" \
          "O patch não pôde ser mesclado na área isolada. A VPS live permaneceu no commit anterior." \
          "${LAST_ERROR_STDERR:-git apply 3-way falhou}"
      fi
      log_update_operation_timing_ms "preparation.apply_patch" "$prepare_started_ms"
    else
      prepare_started_ms="$(update_now_ms)"
      if ! apply_local_candidate_operations; then
        log_update_operation_timing_ms "preparation.operations" "$prepare_started_ms"
        reject_local_candidate_safely \
          "Atualização bloqueada" \
          "Uma operação declarativa não pôde ser aplicada na área isolada. A VPS live não foi alterada." \
          "${LAST_ERROR_STDERR:-falha em operação declarativa}"
      fi
      log_update_operation_timing_ms "preparation.operations" "$prepare_started_ms"

      prepare_started_ms="$(update_now_ms)"
      if ! copy_local_candidate_files; then
        log_update_operation_timing_ms "preparation.copy_files" "$prepare_started_ms"
        LAST_ERROR_CODE="CANDIDATE_COPY_FAILED"
        LAST_ERROR_STDERR="não foi possível copiar os arquivos do candidato para o worktree isolado"
        reject_local_candidate_safely \
          "Falha ao aplicar atualização" \
          "Os arquivos não puderam ser preparados na área isolada. A VPS live não foi alterada." \
          "$LAST_ERROR_STDERR"
      fi
      log_update_operation_timing_ms "preparation.copy_files" "$prepare_started_ms"
      # Compatibilidade exclusiva com candidatos schema v2 antigos. Mesmo esse
      # hook legado agora roda dentro do worktree, nunca diretamente no checkout
      # live antes da validação estática.
      local apply_repo
      apply_repo="$(candidate_repo_dir)"
      if (( LOCAL_CANDIDATE_SCHEMA_VERSION < 3 )) && [[ -f "$apply_repo/scripts/migrate-dashboard-layout.sh" ]]; then
        prepare_started_ms="$(update_now_ms)"
        sudo -u ubuntu -H env REPO_DIR="$apply_repo" bash "$apply_repo/scripts/migrate-dashboard-layout.sh" --apply --stage
        log_update_operation_timing_ms "preparation.legacy_migration" "$prepare_started_ms"
      fi
      prepare_started_ms="$(update_now_ms)"
      git_add_changed_files_or_reject "git add do candidato isolado"
      log_update_operation_timing_ms "preparation.git_add" "$prepare_started_ms"
    fi

    prepare_started_ms="$(update_now_ms)"
    if ! refresh_changed_files_from_staged_diff; then
      log_update_operation_timing_ms "preparation.staged_diff" "$prepare_started_ms"
      LAST_ERROR_CODE="CANDIDATE_STAGED_DIFF_FAILED"
      reject_local_candidate_safely \
        "Falha ao validar atualização" \
        "Não consegui calcular o diff staged na área isolada. A VPS live não foi alterada." \
        "${LAST_ERROR_STDERR:-falha ao ler diff staged}"
    fi
    log_update_operation_timing_ms "preparation.staged_diff" "$prepare_started_ms"
    if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
      discard_local_candidate_worktree || true
      notify_zip_status_message "success" "Nenhuma alteração necessária" "O pacote já corresponde ao estado atual da VPS. Nenhum arquivo foi modificado." || true
      archive_local_candidate "done"
      logger -t "$LOG_TAG" "Candidato local não produziu diff no worktree isolado"
      trigger_updater_if_queue_pending
      exit 0
    fi

    classify_changed_files
    zip_progress_done_and_publish "Isolamento preparado" "Validando candidato"
    if ! prepare_local_candidate_commit_in_worktree; then
      reject_local_candidate_safely \
        "Atualização rejeitada no staging" \
        "O candidato falhou antes de tocar a árvore live. Nenhuma promoção foi realizada." \
        "${LAST_ERROR_STDERR:-preflight/commit isolado falhou}"
    fi

    STAGE="preparação de artefatos no worktree"
    set_updater_priority_profile safe
    prepare_started_ms="$(update_now_ms)"
    if ! prepare_local_candidate_runtime_artifacts_in_worktree; then
      log_update_operation_timing_ms "preparation.runtime_artifacts" "$prepare_started_ms"
      reject_local_candidate_safely \
        "Atualização rejeitada no build isolado" \
        "Testes ou builds falharam antes de tocar a árvore live. Nenhuma promoção foi realizada." \
        "${LAST_ERROR_STDERR:-falha ao preparar artefatos isolados}"
    fi
    log_update_operation_timing_ms "preparation.runtime_artifacts" "$prepare_started_ms"
  fi
  mark_update_timing "candidate_apply"

  # Snapshot pode copiar árvores/runtime e portanto permanece em baixa prioridade.
  set_updater_priority_profile safe
  STAGE="preservação do runtime anterior"
  prepare_started_ms="$(update_now_ms)"
  if ! capture_runtime_release_snapshot "$PREVIOUS_COMMIT"; then
    log_update_operation_timing_ms "preparation.rollback_snapshot" "$prepare_started_ms"
    reject_local_candidate_safely \
      "Atualização não promovida" \
      "O candidato ficou READY, mas o runtime atual não pôde ser preservado para rollback sem rebuild. A árvore live permaneceu no commit anterior." \
      "${LAST_ERROR_STDERR:-falha ao preservar release runtime anterior}"
  fi
  log_update_operation_timing_ms "preparation.rollback_snapshot" "$prepare_started_ms"
  zip_progress_done_and_publish "Candidato READY em isolamento" "Promovendo para a VPS"

  set_updater_priority_profile fast
  if ! promote_local_candidate_worktree_commit; then
    set_updater_priority_profile safe
    reject_local_candidate_safely \
      "Atualização não promovida" \
      "O candidato passou pelo staging, mas o checkout live mudou ou não pôde receber o fast-forward. Nenhuma promoção parcial foi mantida." \
      "${LAST_ERROR_STDERR:-promoção do candidato falhou}"
  fi
  mark_update_timing "candidate_promote"
  set_updater_priority_profile safe
  zip_progress_done "Promovido para a VPS"

}

publish_local_candidate_after_validation() {
  if (( LOCAL_CANDIDATE_MODE == 0 )); then
    return 0
  fi
  if (( LOCAL_CANDIDATE_PUBLISHED == 1 )); then
    return 0
  fi

  STAGE="confirmação do commit local validado"
  local live_head
  live_head="$(repo_git rev-parse HEAD)"
  if [[ -z "${LOCAL_CANDIDATE_PREPARED_COMMIT:-}" ]]; then
    LOCAL_CANDIDATE_PREPARED_COMMIT="$live_head"
  fi
  if [[ "$live_head" != "$LOCAL_CANDIDATE_PREPARED_COMMIT" ]]; then
    LAST_ERROR_STDERR="HEAD live divergiu do commit preparado antes do push: esperado $(short_commit "$LOCAL_CANDIDATE_PREPARED_COMMIT"), atual $(short_commit "$live_head")"
    LAST_ERROR_CODE="LIVE_HEAD_CHANGED_BEFORE_PUSH"
    return 1
  fi

  REMOTE_COMMIT="$live_head"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  write_local_candidate_state "validated" "$REMOTE_COMMIT"

  STAGE="push GitHub pós-validação"
  zip_progress_publish "Publicando no GitHub..."
  repo_git push origin "HEAD:$BRANCH"
  record_remote_fetch_state "$REMOTE_COMMIT" || true
  LOCAL_CANDIDATE_PUBLISHED=1
  # A partir daqui o remoto já contém o commit validado. Qualquer falha
  # subsequente é de finalização e não pode resetar somente a VPS.
  mark_deployment_committed
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}
