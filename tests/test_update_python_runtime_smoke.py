from __future__ import annotations

import json
from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core
import shutil
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "updater" / "utilitarios" / "smoke_runtime.py"
UPDATER = caminho_fonte_core()


def _write_fake_project(tmp_path: Path, *, cog_source: str, setup_source: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "cogs").mkdir(parents=True)
    (root / "cogs" / "__init__.py").write_text("", encoding="utf-8")
    (root / "bot.py").write_text(
        textwrap.dedent(
            """
            from pathlib import Path

            EXPLICIT_COG_EXTENSIONS = ("cogs.alpha",)

            class BotLocal:
                def _read_critical_extensions(self):
                    return {"cogs.alpha"}

                def _discover_cog_extensions(self):
                    return ["cogs.alpha"]
            """
        ),
        encoding="utf-8",
    )
    if setup_source is None:
        setup_source = "async def setup(bot):\n    return None\n"
    (root / "cogs" / "alpha.py").write_text(cog_source + "\n" + setup_source, encoding="utf-8")
    return root


def _run_smoke(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SMOKE), "--root", str(root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_smoke_fake_project_imports_loader_extensions_without_starting_bot(tmp_path: Path):
    root = _write_fake_project(tmp_path, cog_source="VALUE = 1")
    result = _run_smoke(root)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["extensions"] == 1
    assert payload["extension_names"] == ["cogs.alpha"]
    assert payload["critical_extensions"] == ["cogs.alpha"]


def test_smoke_rejects_extension_import_error(tmp_path: Path):
    root = _write_fake_project(tmp_path, cog_source="raise ImportError('boom runtime')")
    result = _run_smoke(root)
    assert result.returncode != 0
    assert "falha ao importar extensão cogs.alpha" in result.stderr
    assert "boom runtime" in result.stderr


def test_smoke_rejects_non_async_setup(tmp_path: Path):
    root = _write_fake_project(
        tmp_path,
        cog_source="VALUE = 1",
        setup_source="def setup(bot):\n    return None\n",
    )
    result = _run_smoke(root)
    assert result.returncode != 0
    assert "setup() não assíncrono" in result.stderr


def test_smoke_rejects_critical_cog_missing_from_loader(tmp_path: Path):
    root = _write_fake_project(tmp_path, cog_source="VALUE = 1")
    bot = root / "bot.py"
    text = bot.read_text(encoding="utf-8").replace(
        'return ["cogs.alpha"]',
        'return ["cogs.beta"]',
    )
    (root / "cogs" / "beta.py").write_text("async def setup(bot):\n    return None\n", encoding="utf-8")
    bot.write_text(text, encoding="utf-8")
    result = _run_smoke(root)
    assert result.returncode != 0
    assert "cog(s) crítica(s) não aparecem no loader" in result.stderr


def test_updater_runs_runtime_smoke_before_frontend_backend_builds_and_ready_manifest():
    text = UPDATER.read_text(encoding="utf-8")
    fn_start = text.index("prepare_local_candidate_runtime_artifacts_in_worktree()")
    fn_end = text.index("promote_local_candidate_worktree_commit()", fn_start)
    block = text[fn_start:fn_end]

    smoke = block.index('run_candidate_python_runtime_smoke "$validation_worktree"')
    frontend = block.index('STAGE="dependências do frontend"')
    backend = block.index('STAGE="dependências do backend"')
    ready = block.index("write_local_candidate_artifact_ready_manifest")
    assert smoke < frontend
    assert smoke < backend
    assert smoke < ready


def test_runtime_smoke_accepts_candidate_interpreter_without_discord_connection():
    text = UPDATER.read_text(encoding="utf-8")
    start = text.index("run_candidate_python_runtime_smoke()")
    end = text.index("prepare_local_candidate_runtime_artifacts_in_worktree()", start)
    block = text[start:end]

    assert 'local py="${2:-}"' in block
    assert 'py="$(current_bot_python_bin)"' in block
    assert 'PYTHONPATH=%q' in block
    assert 'PYTHONDONTWRITEBYTECODE=1' in block
    assert 'updater/utilitarios/smoke_runtime.py' in block
    assert 'timeout %qs' in block
    assert 'BOT_RUNTIME_SMOKE_FAILED' in block
    assert 'BOT_RUNTIME_SMOKE_TIMEOUT' in block
    assert "bot.start" not in block


def test_requirements_change_prepares_isolated_python_runtime_before_smoke():
    text = UPDATER.read_text(encoding="utf-8")
    start = text.index("prepare_local_candidate_runtime_artifacts_in_worktree()")
    end = text.index("promote_local_candidate_worktree_commit()", start)
    block = text[start:end]

    prepare = block.index('prepare_candidate_python_runtime "$validation_worktree" "$artifact_commit"')
    smoke = block.index('run_candidate_python_runtime_smoke "$validation_worktree" "$smoke_py"')
    assert prepare < smoke
    assert 'smoke_py="$LOCAL_CANDIDATE_PYTHON_ARTIFACT/venv/bin/python"' in block

    deploy_start = text.index("deploy_bot()")
    deploy_end = text.index("frontend_publication_is_healthy()", deploy_start)
    deploy = text[deploy_start:deploy_end]
    transactional = deploy[deploy.index("if (( REQUIREMENTS_CHANGED == 1") : deploy.index("if (( BOT_CHANGED == 1")]
    assert "activate_python_runtime_release" in transactional
    assert '$REPO_DIR/.venv/bin/pip' not in transactional


def test_success_report_includes_candidate_runtime_status():
    text = UPDATER.read_text(encoding="utf-8")
    assert "${RUNTIME_CHECK_MARK} Runtime candidato — ${PREFLIGHT_RUNTIME_STATUS}" in text
    assert 'RUNTIME_CHECK_MARK="•"' in text
    assert 'PREFLIGHT_RUNTIME_STATUS="validado na execução anterior"' in text
