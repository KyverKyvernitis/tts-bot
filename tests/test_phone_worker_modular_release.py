"""Build local release fixtures and exercise the original bootstrap 1.0.0."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"
BOOTSTRAP_100 = ROOT / "tests/fixtures/phone_worker_bootstrap_1_0_0.py"
BOOTSTRAP_SHA = "c103058fab021b591e6af384379da8183646a9a29033160c96791bebcd829aa5"
NEW_FILES = {"phone_worker_runtime/__init__.py", "phone_worker_runtime/config.py",
             "phone_worker_runtime/telemetry.py", "phone_worker_runtime/control_plane.py",
             "phone_worker_runtime/voice_state.py", "phone_worker_runtime/tts_policy.py", "phone_worker_runtime/tts_cache.py", "phone_worker_runtime/tts_android.py", "phone_worker_runtime/tts_providers.py", "phone_worker_runtime/pcm_io.py",
             "cogs/__init__.py", "cogs/musica/__init__.py",
             "cogs/musica/runtime_telefone/__init__.py",
             "cogs/musica/runtime_telefone/agente/__init__.py",
             "cogs/musica/runtime_telefone/agente/configuracao.py",
             "cogs/musica/runtime_telefone/agente/ciclo_vida.py",
             "cogs/musica/runtime_telefone/agente/utilitarios.py",
             "cogs/musica/runtime_telefone/agente/estado.py",
             "cogs/musica/runtime_telefone/agente/mixer_pcm.py",
             "cogs/musica/runtime_telefone/agente/resolucao.py",
             "cogs/musica/runtime_telefone/agente/reproducao.py",
             "cogs/musica/runtime_telefone/agente/tts.py",
             "cogs/musica/runtime_telefone/agente/servidor.py",
             "cogs/musica/runtime_telefone/ponte_worker/__init__.py",
             "cogs/musica/runtime_telefone/ponte_worker/configuracao.py",
             "cogs/musica/runtime_telefone/ponte_worker/streams.py",
             "cogs/musica/runtime_telefone/ponte_worker/resolucao.py",
             "cogs/musica/runtime_telefone/ponte_worker/proxy.py",
             "cogs/musica/runtime_telefone/ponte_worker/telemetria.py",
             "cogs/musica/runtime_telefone/ponte_worker/servico.py",
             "cogs/musica/runtime_telefone/termux/__init__.py",
             "cogs/musica/runtime_telefone/termux/integracao-worker.sh",
             "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh",
             "cogs/musica/runtime_telefone/termux/musica.env.example"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    module = load("phone_modular_release_publisher", ROOT / "scripts/core-worker-automation.py")
    monkeypatch.setattr(module, "AGENT_RELEASE_ROOT", tmp_path / "release")
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://vps.invalid")
    return module


def original_bootstrap():
    assert hashlib.sha256(BOOTSTRAP_100.read_bytes()).hexdigest() == BOOTSTRAP_SHA
    module = load("phone_original_bootstrap_100", BOOTSTRAP_100)
    assert module.BOOTSTRAP_VERSION == "1.0.0" and module.DEFAULT_MAX_MEMBERS == 64
    return module


def source_copy(tmp_path, monkeypatch, publisher):
    root = tmp_path / "source"
    phone = root / "deploy/termux/phone-worker"
    shutil.copytree(PHONE, phone, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "cogs/musica/runtime_telefone", root / "cogs/musica/runtime_telefone")
    shutil.copy2(ROOT / "cogs/__init__.py", root / "cogs/__init__.py")
    shutil.copy2(ROOT / "cogs/musica/__init__.py", root / "cogs/musica/__init__.py")
    # Espelha o layout instalado para o hash calculado pelo próprio worker.
    shutil.copytree(root / "cogs", phone / "cogs")
    monkeypatch.setattr(publisher, "ROOT", root)
    monkeypatch.setattr(publisher, "PHONE_WORKER_CANONICAL_ROOT", phone.resolve())
    return phone


def test_complete_modular_release_is_accepted_by_original_bootstrap(publisher, tmp_path):
    bootstrap = original_bootstrap()
    payload = publisher._build_worker_update_payload()
    latest = publisher._publish_phone_worker_release(payload)
    outer = bootstrap._validate_manifest(latest, "https://vps.invalid")
    archive = publisher.AGENT_RELEASE_ROOT / "releases" / (outer["source_hash"] + ".zip")
    with zipfile.ZipFile(archive) as z:
        members = set(z.namelist())
        assert NEW_FILES <= members
        assert members == {name for name, _ in publisher.PHONE_WORKER_FILES} | {"phone-worker-release.json"}
        assert len(z.infolist()) == len(publisher.PHONE_WORKER_FILES) + 1 <= bootstrap.DEFAULT_MAX_MEMBERS
        assert sum(info.file_size for info in z.infolist()) <= bootstrap.DEFAULT_MAX_EXPANDED_BYTES
    assert archive.stat().st_size <= bootstrap.DEFAULT_MAX_ARCHIVE_BYTES
    staging = tmp_path / "staging"
    extracted = bootstrap._extract_and_validate(archive, staging, outer)
    assert extracted["members"] == len(publisher.PHONE_WORKER_FILES)
    for name, mode in publisher.PHONE_WORKER_FILES:
        assert (staging / name).read_bytes() == publisher._phone_worker_source_path(PHONE, name).read_bytes()
        assert (staging / name).stat().st_mode & 0o777 == mode
    # Import the extracted entrypoint from an unrelated working directory,
    # with site-packages disabled: no accidental imports from the source tree.
    env = os.environ.copy()
    env.update(PHONE_WORKER_RELEASE_DIR=str(staging), PHONE_WORKER_DIR=str(tmp_path / "install"),
               PHONE_WORKER_ENV=str(tmp_path / "absent.env"), PHONE_WORKER_STATE_DIR=str(tmp_path / "state"),
               MUSIC_AGENT_AUTO_TOKEN="0", PYTHONDONTWRITEBYTECODE="1")
    code = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("release_phone", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
assert worker._PHONE_WORKER_CONFIG_MODULE is None
assert worker._PHONE_WORKER_TELEMETRY_MODULE is None
assert worker._PHONE_WORKER_CONTROL_PLANE_MODULE is None
assert worker._PHONE_WORKER_VOICE_STATE_MODULE is None
assert worker._PHONE_WORKER_TTS_POLICY_MODULE is None
assert worker._PHONE_WORKER_TTS_CACHE_MODULE is None
assert worker._PHONE_WORKER_TTS_ANDROID_MODULE is None
assert worker._PHONE_WORKER_TTS_PROVIDERS_MODULE is None
assert worker._PHONE_WORKER_PCM_IO_MODULE is None
assert worker._safe_env_key(" CONFIG_KEY ") == "CONFIG_KEY"
assert worker._phone_worker_source_hash() == sys.argv[2]
worker._run_json_command = lambda *a, **kw: {"level": 37}
assert worker._battery_snapshot()["level"] == 37
assert worker._phone_worker_telemetry_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_control_plane_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_voice_state_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_tts_policy_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_tts_cache_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_tts_android_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_tts_providers_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._phone_worker_pcm_io_module().__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._PHONE_WORKER_MUSIC_BRIDGE_MODULES == {}
music_streams = worker._phone_worker_music_bridge_module("streams")
assert music_streams._MUSIC_PCM_PREPARATIONS == {}
assert music_streams.__file__.startswith(str(worker.Path(sys.argv[1]).parent))
assert worker._tts_agent_normalize_engine("google-cloud") == "gtts"
assert worker._TETO_RENDERER is None and worker._APK_IDENTITY_MODULE is None
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(staging / "phone_worker.py"), outer["source_hash"]],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    help_result = subprocess.run([sys.executable, "-S", str(staging / "phone_worker.py"), "--help"],
                                 env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr


@pytest.mark.parametrize("module_file", ["config.py", "telemetry.py", "control_plane.py", "voice_state.py", "tts_policy.py", "tts_cache.py", "tts_android.py", "tts_providers.py", "pcm_io.py"])
def test_module_only_edit_changes_runtime_hash_and_immutable_release(publisher, tmp_path, monkeypatch, module_file):
    phone = source_copy(tmp_path, monkeypatch, publisher)
    worker = load("phone_source_hash_live", phone / "phone_worker.py")
    monkeypatch.setenv("PHONE_WORKER_RELEASE_DIR", str(phone))
    first = publisher._publish_phone_worker_release(publisher._build_worker_update_payload())
    assert worker._phone_worker_source_hash() == first["source_hash"]
    first_zip = publisher.AGENT_RELEASE_ROOT / "releases" / (first["source_hash"] + ".zip")
    original = first_zip.read_bytes()
    module = phone / "phone_worker_runtime" / module_file
    module.write_text(module.read_text() + "\n# module-only release edit\n")
    second = publisher._publish_phone_worker_release(publisher._build_worker_update_payload())
    assert second["source_hash"] != first["source_hash"]
    assert worker._phone_worker_source_hash() == second["source_hash"]
    assert second["sha256"] != first["sha256"] and second["url"] != first["url"]
    assert first_zip.read_bytes() == original


@pytest.mark.parametrize("missing", sorted(NEW_FILES))
def test_missing_new_module_prevents_release(publisher, tmp_path, monkeypatch, missing):
    phone = source_copy(tmp_path, monkeypatch, publisher)
    source_path = publisher._phone_worker_source_path(phone, missing)
    source_path.unlink()
    if missing in publisher.PHONE_WORKER_SOURCE_HASH_EXCLUDED:
        assert publisher._hash_phone_worker_files(phone)
    else:
        assert publisher._hash_phone_worker_files(phone) == ""
    with pytest.raises(RuntimeError, match="arquivos obrigatórios"):
        publisher._build_worker_update_payload()
    assert not publisher.AGENT_RELEASE_ROOT.exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_module_symlink_cannot_publish_bytes_outside_source(publisher, tmp_path, monkeypatch, ancestor):
    phone = source_copy(tmp_path, monkeypatch, publisher)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.py").write_text("PRIVATE_OUTSIDE = True\n")
    (outside / "__init__.py").write_text("# outside\n")
    if ancestor:
        shutil.rmtree(phone / "phone_worker_runtime")
        (phone / "phone_worker_runtime").symlink_to(outside, target_is_directory=True)
    else:
        (phone / "phone_worker_runtime/config.py").unlink()
        (phone / "phone_worker_runtime/config.py").symlink_to(outside / "config.py")
    with pytest.raises(ValueError, match="link|raiz"):
        publisher._build_worker_update_payload()
    with pytest.raises(ValueError, match="link|raiz"):
        publisher._hash_phone_worker_files(phone)
    assert not publisher.AGENT_RELEASE_ROOT.exists()


def inline_files(count, raw=b"# harmless fixture\n"):
    return {"version": "1.11.6", "source_hash": "c" * 64, "files": [
        {"target": f"module/file_{i}.py", "mode": 0o644, "sha256": hashlib.sha256(raw).hexdigest(),
         "data_b64": base64.b64encode(raw).decode()} for i in range(count)]}


def test_publisher_counts_internal_manifest_in_old_bootstrap_member_budget(publisher):
    bootstrap = original_bootstrap()
    with pytest.raises(ValueError, match="membros"):
        publisher._publish_phone_worker_release(inline_files(bootstrap.DEFAULT_MAX_MEMBERS))
    assert not publisher.AGENT_RELEASE_ROOT.exists()


@pytest.mark.parametrize("target", ["phone-worker-release.json", "module/file\x00alias.py"])
def test_publisher_rejects_reserved_manifest_and_truncated_zip_names(publisher, target):
    payload = inline_files(1)
    payload["files"][0]["target"] = target
    with pytest.raises(ValueError, match="caminho inválido"):
        publisher._publish_phone_worker_release(payload)
    assert not publisher.AGENT_RELEASE_ROOT.exists()


@pytest.mark.parametrize("budget", ["EXPANDED", "ARCHIVE"])
def test_oversized_release_does_not_replace_latest_or_leave_temporary(publisher, monkeypatch, budget):
    latest = publisher._publish_phone_worker_release(publisher._build_worker_update_payload())
    latest_bytes = (publisher.AGENT_RELEASE_ROOT / "latest.json").read_bytes()
    monkeypatch.setattr(publisher, "PHONE_WORKER_RELEASE_MAX_" + budget + "_BYTES", 16, raising=False)
    with pytest.raises(ValueError, match="limite"):
        publisher._publish_phone_worker_release(inline_files(1, os.urandom(256)))
    assert (publisher.AGENT_RELEASE_ROOT / "latest.json").read_bytes() == latest_bytes
    release_dir = publisher.AGENT_RELEASE_ROOT / "releases"
    assert not list(release_dir.glob("*.tmp"))
    assert not (release_dir / ("c" * 64 + ".zip")).exists()
    assert (release_dir / (latest["source_hash"] + ".zip")).is_file()
