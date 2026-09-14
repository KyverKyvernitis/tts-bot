write_local_candidate_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local state_name="${1:-state}"
  local extra_commit="${2:-}"
  python3 - "$LOCAL_CANDIDATE_DIR" "$state_name" "$extra_commit" <<'PYSTATE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
state = sys.argv[2]
commit = sys.argv[3]
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data.update({
    "state": state,
    "commit": commit,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, path)
PYSTATE
}

write_local_candidate_recovery_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local rollback_ok="${1:-false}"
  local restored_commit="${2:-}"
  local target_commit="${3:-}"
  local failure_code="${4:-UPDATE_STAGE_FAILED}"
  local failed_stage="${5:-}"
  local recovery_duration="${6:-}"
  local bot_health="${7:-}"
  ROLLBACK_OK_VALUE="$rollback_ok" RESTORED_COMMIT_VALUE="$restored_commit" \
  TARGET_COMMIT_VALUE="$target_commit" FAILURE_CODE_VALUE="$failure_code" \
  FAILED_STAGE_VALUE="$failed_stage" RECOVERY_DURATION_VALUE="$recovery_duration" \
  BOT_HEALTH_VALUE="$bot_health" python3 - "$LOCAL_CANDIDATE_DIR" <<'PYRECOVERYSTATE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
rollback_ok = (os.environ.get("ROLLBACK_OK_VALUE") or "false").lower() == "true"
data.update({
    "state": "failed",
    "commit": os.environ.get("RESTORED_COMMIT_VALUE") or "",
    "target_commit": os.environ.get("TARGET_COMMIT_VALUE") or "",
    "rollback_ok": rollback_ok,
    "recovery_state": "restored" if rollback_ok else "incomplete",
    "failure_code": os.environ.get("FAILURE_CODE_VALUE") or "UPDATE_STAGE_FAILED",
    "failed_stage": os.environ.get("FAILED_STAGE_VALUE") or "",
    "recovery_duration": os.environ.get("RECOVERY_DURATION_VALUE") or "",
    "bot_health": os.environ.get("BOT_HEALTH_VALUE") or "",
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, path)
PYRECOVERYSTATE
}
