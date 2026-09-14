from __future__ import annotations

import os
import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core

ROOT = Path(__file__).resolve().parents[1]
UPDATER = caminho_fonte_core()


def _run(script: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-Eeuo", "pipefail", "-c", script],
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )


def _helper_file(tmp_path: Path) -> Path:
    source = UPDATER.read_text(encoding="utf-8")
    start = source.index("record_remote_fetch_state() {")
    end = source.index("\n\ncurrent_bot_python_bin() {", start)
    helper = tmp_path / "fetch_helpers.sh"
    helper.write_text(source[start:end] + "\n", encoding="utf-8")
    return helper


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    bare = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "one"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, commit


def _common(repo: Path, state: Path, calls: Path, ttl: int) -> str:
    return f'''REPO_DIR="{repo}"
BRANCH=main
REMOTE_FETCH_STATE_FILE="{state}"
LOCAL_FETCH_REUSE_SECONDS={ttl}
LOG_TAG=test
sanitize_commit_ref() {{ [[ "$1" =~ ^[0-9a-fA-F]{{40}}$ ]] && printf '%s\\n' "$1"; }}
short_commit() {{ printf '%.7s' "$1"; }}
logger() {{ :; }}
repo_git() {{
  if [[ "${{1:-}}" == fetch ]]; then printf 'fetch\\n' >> "{calls}"; return 0; fi
  git -C "$REPO_DIR" "$@"
}}
'''


def test_recent_matching_fetch_skips_network_fetch(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    helper = _helper_file(tmp_path)
    state = tmp_path / "fetch.tsv"
    calls = tmp_path / "calls.log"
    result = _run(
        f'''source "{helper}"
{_common(repo, state, calls, 30)}
record_remote_fetch_state "{commit}"
fetch_remote_for_local_candidate
printf 'REUSED=%s\\n' "$REMOTE_FETCH_REUSED"
if [[ -f "{calls}" ]]; then printf 'CALLS=%s\\n' "$(wc -l < "{calls}")"; else printf 'CALLS=0\\n'; fi
''',
        cwd=repo,
    )
    assert "REUSED=1" in result.stdout
    assert "CALLS=0" in result.stdout


def test_stale_fetch_state_performs_real_fetch(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    helper = _helper_file(tmp_path)
    state = tmp_path / "fetch.tsv"
    calls = tmp_path / "calls.log"
    state.write_text(f"1\tmain\t{commit}\n", encoding="utf-8")
    result = _run(
        f'''source "{helper}"
{_common(repo, state, calls, 15)}
fetch_remote_for_local_candidate
printf 'REUSED=%s\\n' "$REMOTE_FETCH_REUSED"
printf 'CALLS=%s\\n' "$(wc -l < "{calls}")"
''',
        cwd=repo,
    )
    assert "REUSED=0" in result.stdout
    assert "CALLS=1" in result.stdout


def test_matching_age_but_mismatched_tracking_ref_forces_fetch(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    helper = _helper_file(tmp_path)
    state = tmp_path / "fetch.tsv"
    calls = tmp_path / "calls.log"
    fake = "f" * 40 if commit != "f" * 40 else "e" * 40
    now = int(subprocess.check_output(["date", "+%s"], text=True).strip())
    state.write_text(f"{now}\tmain\t{fake}\n", encoding="utf-8")
    result = _run(
        f'''source "{helper}"
{_common(repo, state, calls, 30)}
fetch_remote_for_local_candidate
printf 'REUSED=%s\\n' "$REMOTE_FETCH_REUSED"
printf 'CALLS=%s\\n' "$(wc -l < "{calls}")"
''',
        cwd=repo,
    )
    assert "REUSED=0" in result.stdout
    assert "CALLS=1" in result.stdout


def test_remote_polling_still_fetches_and_push_refreshes_state() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    main = source[source.index("trap 'cleanup_runtime_artifacts' EXIT"):]
    assert 'repo_git fetch origin "$BRANCH"' in main
    start = source.index("publish_local_candidate_after_validation() {")
    end = source.index("\n\nformat_changed_files() {", start)
    local_publish = source[start:end]
    assert 'repo_git push origin "HEAD:$BRANCH"\n  record_remote_fetch_state "$REMOTE_COMMIT" || true' in local_publish
