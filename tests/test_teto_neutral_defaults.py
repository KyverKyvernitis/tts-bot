"""Keep the bot's neutral Teto defaults aligned with the WORLDLINE test WAV."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_tts_helpers import QueueItem, tts_audio
from test_tts_message_flow import FakeCog, FakeDB, make_message
from test_tts_modularizacao_interface_modais_vozes_online import _carregar_modulo
from test_tts_modularizacao_interface_paineis_principais import _carregar_lancador

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("override, expected", ((None, 0.0), ("bad", 0.0), ("-2.0", -2.0), ("1,5", 1.5)))
def test_default_is_neutral_and_explicit_operator_pitch_is_preserved(override, expected):
    environment = dict(os.environ)
    environment.pop("TTS_TETO_DEFAULT_PITCH_SEMITONES", None)
    if override is not None:
        environment["TTS_TETO_DEFAULT_PITCH_SEMITONES"] = override
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.TTS_TETO_DEFAULT_PITCH_SEMITONES)"],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=True, timeout=10,
    )
    assert float(result.stdout.strip()) == expected


def test_queue_default_and_malformed_request_use_neutral_pitch(monkeypatch):
    monkeypatch.setattr(tts_audio, "TTS_TETO_DEFAULT_PITCH_SEMITONES", 0.0)
    item = QueueItem(guild_id=1, channel_id=2, author_id=3, text="Olá.", engine="teto",
                     voice="kasane-teto-standard", language="pt-br", rate="1.0", pitch="C4")
    assert float(item.teto_pitch_semitones) == 0.0
    assert tts_audio.TTSAudioMixin._normalize_teto_pitch_semitones(None, "invalid") == "+0.0"
    assert tts_audio.TTSAudioMixin._normalize_teto_pitch_semitones(None, "-1.5") == "-1.5"


@pytest.mark.parametrize("saved_pitch, expected", ((None, 0.0), ("0.0", 0.0), ("-1.5", -1.5)))
def test_teto_message_uses_neutral_rate_and_keeps_saved_pitch(monkeypatch, saved_pitch, expected):
    from cogs.tts.mensagens import preparacao

    monkeypatch.setattr(preparacao.config, "TTS_TETO_DEFAULT_PITCH_SEMITONES", 0.0, raising=False)
    resolved = {"engine": "edge", "rate": "+25%", "pitch": "+20Hz"}
    if saved_pitch is not None:
        resolved["teto_pitch_semitones"] = saved_pitch
    cog = FakeCog(db=FakeDB(resolved=resolved))
    payload = asyncio.run(preparacao.preparar_payload_tts_mensagem(
        cog, make_message("'Olá."), guild_defaults={}, active_prefix="'", forced_engine="teto",
    ))
    assert payload is not None
    assert payload.queue_item.engine == "teto"
    assert payload.queue_item.rate == "1.0"
    assert payload.queue_item.pitch == "C4"
    assert float(payload.queue_item.teto_pitch_semitones) == expected
    assert payload.queue_item.piper_fallback_rate == "+25%"
    assert payload.queue_item.piper_fallback_pitch == "+20Hz"


def _settings_db():
    """Load the real resolver without constructing a MongoDB client."""
    motor = ModuleType("motor")
    motor_asyncio = ModuleType("motor.motor_asyncio")
    motor_asyncio.AsyncIOMotorClient = object
    spec = importlib.util.spec_from_file_location("_teto_neutral_test_db", ROOT / "db.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"motor": motor, "motor.motor_asyncio": motor_asyncio}):
        spec.loader.exec_module(module)
    database = module.SettingsDB.__new__(module.SettingsDB)
    database.user_cache = {}
    database.guild_cache = {}
    database._resolved_tts_cache = {}
    return module, database


@pytest.mark.parametrize("saved_pitch, operator_pitch, expected", (
    (None, 0.0, 0.0), (None, -2.0, -2.0), ("-1.5", 0.0, -1.5), ("0.0", -2.0, 0.0),
))
def test_database_default_does_not_overwrite_personal_or_operator_pitch(monkeypatch, saved_pitch, operator_pitch, expected):
    module, database = _settings_db()
    monkeypatch.setattr(module.config, "TTS_TETO_DEFAULT_PITCH_SEMITONES", operator_pitch, raising=False)
    settings = {"voice": "pt-BR-AntonioNeural", "rate": "+25%"}
    if saved_pitch is not None:
        settings["teto_pitch_semitones"] = saved_pitch
    database.user_cache[(1, 2)] = {"tts": dict(settings)}
    resolved = database.resolve_tts(1, 2)
    assert float(resolved["teto_pitch_semitones"]) == expected
    assert resolved["voice"] == "pt-BR-AntonioNeural"
    assert resolved["rate"] == "+25%"
    assert database.user_cache[(1, 2)]["tts"] == settings


def test_teto_modal_resets_only_pitch_to_neutral():
    module, save = _carregar_modulo()
    module.config.TTS_TETO_DEFAULT_PITCH_SEMITONES = 0.0
    modal = module.ModalConfiguracaoTeto(SimpleNamespace(valores={"teto_pitch_semitones": "-1.5"}), None)
    modal.teto_pitch_semitones.value = "0.0"
    asyncio.run(modal.on_submit(SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))))
    assert save.await_args.kwargs["updates"] == {"teto_pitch_semitones": "+0.0"}


def test_teto_modal_and_launcher_show_neutral_default():
    module, _ = _carregar_modulo()
    module.config.TTS_TETO_DEFAULT_PITCH_SEMITONES = 0.0
    modal = module.ModalConfiguracaoTeto(SimpleNamespace(), None)
    assert float(modal.current_pitch) == 0.0
    launcher = _carregar_lancador()
    launcher.config.TTS_TETO_DEFAULT_PITCH_SEMITONES = 0.0
    view = launcher.VisaoLancadorPublicoTTS(SimpleNamespace(_get_db=lambda: None), 10, 20)
    assert view._tom_teto_atual() == "+0.0"
    view._user_settings = {"teto_pitch_semitones": "-1.5"}
    assert view._tom_teto_atual() == "-1.5"
