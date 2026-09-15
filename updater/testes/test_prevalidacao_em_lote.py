from __future__ import annotations

import os
import subprocess
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core


ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )


def _sudo_wrapper(counter: Path) -> str:
    return f'''\nsudo() {{\n  printf 'call\\n' >> {counter!s}\n  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi\n  [[ "${{1:-}}" == '-H' ]] && shift\n  "$@"\n}}\n'''


def test_live_python_preflight_uses_one_interpreter_for_multiple_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("B = 2\n", encoding="utf-8")
    (repo / "c.py").write_text("C = 3\n", encoding="utf-8")
    counter = tmp_path / "sudo-count"

    harness = f'''\nsource <(awk '/^run_preflight_checks[(][)]/{{flag=1}} /^verify_bot_after_restart[(][)]/{{flag=0}} flag' {UPDATER!s})\nlogger() {{ :; }}\n{_sudo_wrapper(counter)}\nREPO_DIR={repo!s}\nCHANGED_FILES_RAW=$'a.py\\nb.py\\nc.py'\nCHANGED_STATUS_RAW=$'M\\ta.py\\nM\\tb.py\\nM\\tc.py'\nPREFLIGHT_PY_STATUS=''\nPREFLIGHT_BASH_STATUS=''\nPREFLIGHT_COG_IMPORT_STATUS=''\nUPDATE_HAS_WARNINGS=0\nLOG_TAG='test-updater'\nrun_preflight_checks\nprintf 'PY=%s\\n' "$PREFLIGHT_PY_STATUS"\nprintf 'CALLS=%s\\n' "$(wc -l < {counter!s})"\n'''
    result = _run_bash(harness)
    assert "PY=OK" in result.stdout
    assert "CALLS=1" in result.stdout


def test_cog_import_preflight_batches_multiple_modules_into_one_process(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cogs = repo / "cogs"
    cogs.mkdir(parents=True)
    (cogs / "__init__.py").write_text("\n", encoding="utf-8")
    (cogs / "one.py").write_text("VALUE = 1\n", encoding="utf-8")
    (cogs / "two.py").write_text("VALUE = 2\n", encoding="utf-8")
    counter = tmp_path / "sudo-count"

    harness = f'''\nsource <(awk '/^run_preflight_checks[(][)]/{{flag=1}} /^verify_bot_after_restart[(][)]/{{flag=0}} flag' {UPDATER!s})\nlogger() {{ :; }}\n{_sudo_wrapper(counter)}\nREPO_DIR={repo!s}\nCHANGED_FILES_RAW=$'cogs/one.py\\ncogs/two.py'\nCHANGED_STATUS_RAW=$'M\\tcogs/one.py\\nM\\tcogs/two.py'\nPREFLIGHT_PY_STATUS=''\nPREFLIGHT_BASH_STATUS=''\nPREFLIGHT_COG_IMPORT_STATUS=''\nUPDATE_HAS_WARNINGS=0\nLOG_TAG='test-updater'\nrun_preflight_checks\nprintf 'PY=%s\\n' "$PREFLIGHT_PY_STATUS"\nprintf 'COGS=%s\\n' "$PREFLIGHT_COG_IMPORT_STATUS"\nprintf 'CALLS=%s\\n' "$(wc -l < {counter!s})"\n'''
    result = _run_bash(harness)
    assert "PY=OK" in result.stdout
    assert "COGS=OK" in result.stdout
    # Uma inicialização para compilar todos os .py e uma para importar todas as cogs.
    assert "CALLS=2" in result.stdout


def test_worktree_python_preflight_uses_one_interpreter_for_multiple_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (worktree / name).write_text(f"NAME = {name!r}\n", encoding="utf-8")
    counter = tmp_path / "sudo-count"

    harness = f'''\nsource <(awk '/^run_preflight_checks_in_dir[(][)]/{{flag=1}} /^validate_remote_commit_in_staging[(][)]/{{flag=0}} flag' {UPDATER!s})\nlogger() {{ :; }}\n{_sudo_wrapper(counter)}\nREPO_DIR={repo!s}\nCHANGED_FILES_RAW=$'a.py\\nb.py\\nc.py'\nCHANGED_STATUS_RAW=$'M\\ta.py\\nM\\tb.py\\nM\\tc.py'\nPREFLIGHT_PY_STATUS=''\nPREFLIGHT_BASH_STATUS=''\nPREFLIGHT_COG_IMPORT_STATUS=''\nLOG_TAG='test-updater'\nrun_preflight_checks_in_dir {worktree!s}\nprintf 'PY=%s\\n' "$PREFLIGHT_PY_STATUS"\nprintf 'CALLS=%s\\n' "$(wc -l < {counter!s})"\n'''
    result = _run_bash(harness)
    assert "PY=OK" in result.stdout
    assert "CALLS=1" in result.stdout
    assert not list(worktree.rglob("__pycache__"))


def test_batch_reports_each_invalid_python_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    (worktree / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    harness = f'''\nsource <(awk '/^run_preflight_checks_in_dir[(][)]/{{flag=1}} /^validate_remote_commit_in_staging[(][)]/{{flag=0}} flag' {UPDATER!s})\nlogger() {{ :; }}\nsudo() {{\n  if [[ "${{1:-}}" == '-u' ]]; then shift 2; fi\n  [[ "${{1:-}}" == '-H' ]] && shift\n  "$@"\n}}\nREPO_DIR={repo!s}\nCHANGED_FILES_RAW=$'ok.py\\nbad.py'\nCHANGED_STATUS_RAW=$'M\\tok.py\\nM\\tbad.py'\nPREFLIGHT_PY_STATUS=''\nPREFLIGHT_BASH_STATUS=''\nPREFLIGHT_COG_IMPORT_STATUS=''\nLOG_TAG='test-updater'\nset +e\nrun_preflight_checks_in_dir {worktree!s}\nrc=$?\nset -e\nprintf 'RC=%s\\n' "$rc"\nprintf 'PY=%s\\n' "$PREFLIGHT_PY_STATUS"\n'''
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", harness],
        cwd=ROOT,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert "RC=1" in result.stdout
    assert "PY=falhou" in result.stdout
    assert "bad.py" in result.stderr
    assert "SyntaxError" in result.stderr
