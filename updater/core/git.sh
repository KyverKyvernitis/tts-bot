#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Operações Git e snapshots do checkout.

repo_git() {
  # Toda operação Git no checkout deve usar o dono do repositório. Manter o
  # `-C` aqui evita depender do cwd do serviço e elimina chamadas Git como root.
  sudo -u ubuntu -H git -C "$REPO_DIR" "$@"
}

worktree_git() {
  local root="${1:?}"
  shift
  sudo -u ubuntu -H git -C "$root" "$@"
}

candidate_repo_dir() {
  if [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]]; then
    printf '%s\n' "$LOCAL_CANDIDATE_WORKTREE_DIR"
  else
    printf '%s\n' "$REPO_DIR"
  fi
}

candidate_git() {
  local root
  root="$(candidate_repo_dir)"
  worktree_git "$root" "$@"
}

repo_python_as_ubuntu() {
  sudo -u ubuntu -H python3 "$@"
}

load_git_diff_snapshot() {
  local root="${1:?}"
  shift
  local output
  if ! output="$(repo_python_as_ubuntu "$REPO_DIR/updater/utilitarios/snapshot_git.py" diff --repo "$root" "$@" 2>&1)"; then
    LAST_ERROR_STDERR="${output:-falha ao capturar snapshot Git do diff}"
    return 1
  fi
  eval "$output"
  CHANGED_STATUS_RAW="${GIT_DIFF_STATUS_RAW:-}"
  CHANGED_FILES_RAW="${GIT_DIFF_FILES_RAW:-}"
  CHANGED_DIFF_NUMSTAT_RAW="${GIT_DIFF_NUMSTAT_RAW:-}"
  GIT_DIFF_SNAPSHOT_HASH="${GIT_DIFF_SNAPSHOT_HASH:-}"
}

load_repo_status_snapshot() {
  local output
  if ! output="$(repo_python_as_ubuntu "$REPO_DIR/updater/utilitarios/snapshot_git.py" status --repo "$REPO_DIR" 2>&1)"; then
    GIT_STATUS_SNAPSHOT_READY=0
    LAST_ERROR_STDERR="${output:-falha ao capturar status Git do checkout}"
    return 1
  fi
  eval "$output"
  GIT_STATUS_SNAPSHOT_READY=1
}

load_repo_ref_snapshot() {
  local branch="${1:-$BRANCH}" output
  if ! output="$(repo_python_as_ubuntu "$REPO_DIR/updater/utilitarios/snapshot_git.py" refs --repo "$REPO_DIR" --branch "$branch" 2>&1)"; then
    LAST_ERROR_STDERR="${output:-falha ao capturar refs Git local/remoto}"
    return 1
  fi
  eval "$output"
}

record_remote_fetch_state() {
  local commit="${1:-}" now tmp
  commit="$(sanitize_commit_ref "$commit")"
  [[ -n "$commit" ]] || return 1
  now="$(date +%s)"
  mkdir -p "$(dirname "$REMOTE_FETCH_STATE_FILE")" 2>/dev/null || return 1
  tmp="${REMOTE_FETCH_STATE_FILE}.tmp.$$"
  if ! printf '%s\t%s\t%s\n' "$now" "$BRANCH" "$commit" > "$tmp"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  chmod 0600 "$tmp" 2>/dev/null || true
  if ! mv -f "$tmp" "$REMOTE_FETCH_STATE_FILE"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
}

recent_remote_fetch_commit() {
  local ttl="${LOCAL_FETCH_REUSE_SECONDS:-15}" stamp branch commit now age tracked
  [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=15
  (( ttl > 0 )) || return 1
  (( ttl > 60 )) && ttl=60
  [[ -f "$REMOTE_FETCH_STATE_FILE" && ! -L "$REMOTE_FETCH_STATE_FILE" ]] || return 1
  IFS=$'\t' read -r stamp branch commit < "$REMOTE_FETCH_STATE_FILE" || return 1
  [[ "$stamp" =~ ^[0-9]+$ ]] || return 1
  [[ "$branch" == "$BRANCH" ]] || return 1
  commit="$(sanitize_commit_ref "$commit")"
  [[ -n "$commit" ]] || return 1
  now="$(date +%s)"
  age=$(( now - stamp ))
  (( age >= 0 && age <= ttl )) || return 1
  tracked="$(repo_git rev-parse "origin/$BRANCH" 2>/dev/null || true)"
  [[ "$tracked" == "$commit" ]] || return 1
  printf '%s\n' "$commit"
}

fetch_remote_for_local_candidate() {
  local cached_commit
  REMOTE_FETCH_REUSED=0
  if cached_commit="$(recent_remote_fetch_commit 2>/dev/null)"; then
    REMOTE_FETCH_REUSED=1
    logger -t "$LOG_TAG" "reutilizando fetch remoto recente de $(short_commit "$cached_commit") por até ${LOCAL_FETCH_REUSE_SECONDS}s" 2>/dev/null || true
    return 0
  fi
  repo_git fetch origin "$BRANCH"
}

current_bot_python_bin() {
  local candidate="$PYTHON_RUNTIME_CURRENT_LINK/venv/bin/python"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  candidate="$REPO_DIR/.venv/bin/python"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  command -v python3 || true
}

