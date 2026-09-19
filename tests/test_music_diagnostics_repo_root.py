import importlib
import io
import subprocess
import sys
import types
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_service(monkeypatch):
    # O módulo só precisa que `discord` exista durante o import; os testes abaixo
    # exercitam exclusivamente o diagnóstico/base Git.
    monkeypatch.setitem(sys.modules, "discord", types.ModuleType("discord"))
    sys.modules.pop("cogs.musica.diagnostico.servico", None)
    return importlib.import_module("cogs.musica.diagnostico.servico")


def test_diagnostics_repo_root_points_to_project_root(monkeypatch):
    service = _load_service(monkeypatch)
    assert service.REPO_ROOT == ROOT
    assert (service.REPO_ROOT / "bot.py").is_file()
    assert (service.REPO_ROOT / "cogs").is_dir()


def test_base_archive_uses_git_toplevel_even_when_started_from_music_subdir(tmp_path, monkeypatch):
    service = _load_service(monkeypatch)

    repo = tmp_path / "repo"
    music_dir = repo / "cogs" / "musica"
    music_dir.mkdir(parents=True)
    (repo / "root_file.py").write_text("ROOT = True\n", encoding="utf-8")
    (music_dir / "music_file.py").write_text("MUSIC = True\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "root_file.py", "cogs/musica/music_file.py"], check=True)

    # Reproduz exatamente a regressão: o cwd inicial aponta para cogs/musica.
    monkeypatch.setattr(service, "REPO_ROOT", music_dir)
    monkeypatch.setattr(service, "diagnostics_file_stamp", lambda: "test")

    payload, filename, summary, extra = service.build_git_tracked_base_archive_sync()

    assert filename == "repo-test.zip"
    assert payload is not None, summary
    assert extra == ""
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = set(zf.namelist())

    assert "tts-bot-main/root_file.py" in names
    assert "tts-bot-main/cogs/musica/music_file.py" in names
