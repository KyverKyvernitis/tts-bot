"""Native selection must not import or execute legacy Python provider engines."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_native():
    from cogs.tts.fluxo_rust import _carregar_nucleo
    try:
        native = _carregar_nucleo()
        assert callable(native.provider_languages_json)
    except (ImportError, OSError, AttributeError, AssertionError) as error:
        if os.environ.get("TTS_NATIVE_REQUIRED") == "1":
            pytest.fail(f"native provider/catalog artifact required: {error}")
        pytest.skip(str(error))


def test_full_native_import_and_catalog_without_python_engines():
    code = '''
import importlib.abc
import sys
class BlockPythonEngines(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'gtts', 'edge_tts'}:
            raise AssertionError('imported Python engine: ' + fullname)
sys.meta_path.insert(0, BlockPythonEngines())
from cogs.tts import audio, common, helpers, cog
assert audio.edge_tts is None and audio.gTTS is None
languages = common.get_gtts_languages()
assert languages == helpers.get_gtts_languages()
assert len(languages) == 69 and languages['pt'] == 'Portuguese (Brazil)'
assert not any(name.split('.')[0] in {'gtts', 'edge_tts'} for name in sys.modules)
'''
    environment = dict(os.environ, TTS_BACKEND="rust", PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


class Slots:
    def __init__(self):
        self.held = 0

    async def acquire(self, *, foreground):
        assert self.held == 0
        self.held = 1

    def release(self):
        assert self.held == 1
        self.held = 0


def test_file_fallback_uses_native_provider_and_keeps_domain(monkeypatch, tmp_path):
    from cogs.tts import audio, providers_rust
    slots = Slots()
    target = tmp_path / "voice.mp3"
    requests = []
    monkeypatch.setenv("TTS_PROVIDERS_RUST", "1")

    async def save(request, path):
        assert slots.held == 1
        requests.append(request)
        Path(path).write_bytes(b"native-mp3")
        return {"audio_format": "mp3"}

    monkeypatch.setattr(providers_rust, "save", save)
    monkeypatch.setattr(audio, "gTTS", lambda **kwargs: pytest.fail("executed Python provider"))
    probe = SimpleNamespace(_get_gtts_semaphore=lambda: slots,
                            _make_runtime_temp_file=lambda **kwargs: str(target))
    result = asyncio.run(audio.TTSAudioMixin._generate_gtts_file(
        probe, "Olá", "pt_BR", tld="com.br", foreground=True))
    assert result == str(target) and target.read_bytes() == b"native-mp3"
    assert requests[0]["language"] == "pt" and requests[0]["tld"] == "com.br"
    assert slots.held == 0


def test_native_file_failure_does_not_leave_partial_output_or_slot(monkeypatch, tmp_path):
    from cogs.tts import audio, providers_rust
    slots = Slots()
    target = tmp_path / "partial.mp3"
    monkeypatch.setenv("TTS_PROVIDERS_RUST", "1")

    async def save(request, path):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("incomplete native synthesis")

    monkeypatch.setattr(providers_rust, "save", save)
    probe = SimpleNamespace(_get_gtts_semaphore=lambda: slots,
                            _make_runtime_temp_file=lambda **kwargs: str(target))
    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(audio.TTSAudioMixin._generate_gtts_file(probe, "Olá", "pt"))
    assert slots.held == 0 and not target.exists()
