"""Verifica ganho e segurança da soma com e sem o caminho nativo audioop."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from array import array
from pathlib import Path

import pytest


@pytest.fixture
def mixer_module(monkeypatch):
    discord = types.ModuleType("discord")
    discord.AudioSource = type("AudioSource", (), {})
    monkeypatch.setitem(sys.modules, "discord", discord)
    path = Path(__file__).resolve().parents[4] / "cogs/musica/runtime_telefone/agente/mixer_pcm.py"
    spec = importlib.util.spec_from_file_location("mixer_dsp_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Frames:
    def __init__(self, *frames):
        self.frames = iter(frames)
        self.cleaned = False

    def read(self):
        return next(self.frames, b"")

    def cleanup(self):
        self.cleaned = True


def stereo_frame(left: int, right: int) -> bytes:
    return array("h", [left, right] * 960).tobytes()


def test_telemetria_ignora_silencio_sintetico_do_buffer(mixer_module):
    class Buffered(Frames):
        def read(self):
            frame = super().read()
            self.last_read_had_audio = frame != mixer_module.PCM_SILENCE
            return frame

    wrapper = mixer_module.AgentTelemetryAudioSource(
        Buffered(mixer_module.PCM_SILENCE, mixer_module.PCM_SILENCE, stereo_frame(1000, -1000)),
    )
    try:
        assert wrapper.read() == mixer_module.PCM_SILENCE
        assert wrapper.read() == mixer_module.PCM_SILENCE
        assert wrapper.first_frame_ms is None
        assert wrapper.first_frame_monotonic is None
        assert wrapper.read() == stereo_frame(1000, -1000)
        assert wrapper.first_frame_ms is not None
        assert wrapper.first_frame_monotonic is not None
    finally:
        wrapper.cleanup()


def test_sinal_primeiro_pacote_distingue_tts_silencio_e_fonte_trocada(mixer_module):
    async def scenario():
        class Synthetic(Frames):
            def read(self):
                result = super().read()
                self.last_read_had_audio = False
                return result
        original = Synthetic(mixer_module.PCM_SILENCE)
        replacement = Frames(stereo_frame(1000, -1000))
        mixer = mixer_module.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(), music_source=original,
            music_volume=1, persistent=True,
        )
        wrapper = mixer_module.AgentTelemetryAudioSource(mixer)
        done = mixer.add_tts(Frames(stereo_frame(2000, 2000)), volume=1)
        try:
            assert wrapper.read()
            assert not mixer.last_read_had_music
            assert not wrapper.last_read_had_music
            assert wrapper.last_read_music_source is None
            mixer.replace_music_source(replacement, volume=1, on_music_end=None)
            assert wrapper.read()
            assert wrapper.last_read_had_music
            assert wrapper.last_read_music_source is replacement
            assert mixer.last_read_music_source is replacement
            wrapper.read()
            assert not wrapper.last_read_had_music
            assert wrapper.last_read_music_source is None
            await done
        finally:
            wrapper.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize("native", [True, False])
def test_tts_aceita_200_porcento_sem_aplicar_teto_musical(mixer_module, native):
    async def scenario():
        mixer = mixer_module.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Frames(mixer_module.PCM_SILENCE),
            music_volume=0,
        )
        if not native:
            mixer._audioop_module = None
        elif mixer._audioop() is None:
            pytest.skip("audioop indisponível")
        speech = Frames(stereo_frame(1000, -3000))
        done = mixer.add_tts(speech, volume=2)
        try:
            output = array("h", mixer.read())
            assert set(output[::2]) == {2000}
            assert set(output[1::2]) == {-6000}
            mixer.read()
            await done
            assert speech.cleaned
        finally:
            mixer.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [True, False])
def test_soma_musica_tts_preserva_estereo_sem_clipping(mixer_module, native):
    async def scenario():
        mixer = mixer_module.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Frames(stereo_frame(30000, -30000)),
            music_volume=1,
            duck_factor=1,
        )
        if not native:
            mixer._audioop_module = None
        elif mixer._audioop() is None:
            pytest.skip("audioop indisponível")
        done = mixer.add_tts(Frames(stereo_frame(30000, -30000)), volume=2)
        try:
            output = array("h", mixer.read())
            assert 31995 <= min(output[::2]) <= max(output[::2]) <= 32000
            # audioop arredonda cada parcela negativa para baixo; a soma pode
            # diferir do pico teórico em até dois LSB, ainda longe do clipping.
            assert -32002 <= min(output[1::2]) <= max(output[1::2]) <= -31995
            assert mixer.audio_telemetry()["mix_limited_frames"] == 1
            mixer.read()
            await done
        finally:
            mixer.cleanup()

    asyncio.run(scenario())
