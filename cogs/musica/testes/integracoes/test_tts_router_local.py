from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from types import SimpleNamespace

import discord
import pytest

from cogs.musica.integracoes import tts as integration
from cogs.musica.legado import roteador_audio as routing


FRAME = b"\x01\x00" * 1920


class PCM(discord.AudioSource):
    def __init__(self, frames=(), *, opus=False):
        self.frames = iter(frames)
        self.opus = opus
        self.cleanup_calls = 0

    def read(self):
        return next(self.frames, b"")

    def is_opus(self):
        return self.opus

    def cleanup(self):
        self.cleanup_calls += 1


class Prepared:
    def __init__(self, source, path="/tmp/tts.mp3"):
        self.source = source
        self.path = path
        self.source_kind = "prepared_test"
        self.prime_ms = 12.0

    def take_source(self):
        source, self.source = self.source, None
        return source

    def cleanup(self):
        source = self.take_source()
        if source is not None:
            source.cleanup()


def setup_router(*, mixing=False):
    loop = asyncio.get_running_loop()
    mixer = routing.MixedAudioSource(loop=loop, music_source=PCM([FRAME] * 1000), music_volume=1.0) if mixing else None
    state = routing.MusicGuildState(current_backend="local", current_source=mixer, volume=1.0, volume_loaded=True)
    if mixer is not None:
        mixer.read()

    class Voice:
        source = mixer
        playing = mixing
        connected = True
        channel = SimpleNamespace(id=9)

        def is_connected(self):
            return self.connected

        def is_playing(self):
            return self.playing

        def is_paused(self):
            return False

        def play(self, source, *, after):
            self.source = source
            self.playing = True

            async def consume():
                try:
                    while source.read():
                        await asyncio.sleep(0)
                except Exception as exc:
                    after(exc)
                else:
                    after(None)
                self.playing = False
            asyncio.create_task(consume())

    voice = Voice()
    guild = SimpleNamespace(id=7, voice_client=voice)
    router = routing.AudioRouter.__new__(routing.AudioRouter)
    router._states = {7: state}
    router.bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    return router, state, guild, voice, mixer


async def consume_overlay(mixer, route):
    while not mixer.has_overlays and not route.done():
        await asyncio.sleep(0.001)
    while not route.done():
        mixer.read()
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_prepared_source_is_reused_by_router_without_new_decoder(monkeypatch):
    router, _, guild, voice, _ = setup_router()
    decoder = PCM([FRAME])
    prepared = Prepared(decoder)
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: pytest.fail("decoder duplicado"))
    result = await router.play_tts(guild=guild, vc=voice, path=prepared.path, prepared=prepared)
    assert prepared.source is None
    assert result["source_primed"] is True
    assert result["source_prime_ms"] == 12.0
    assert result["playback_source"] == "prepared_test"
    assert result["first_frame_observed"] is True
    assert result["first_frame_at"] >= result["playback_started_at"]
    assert decoder.cleanup_calls == 1


@pytest.mark.asyncio
async def test_prepared_pcm_is_reused_for_music_overlay_and_ducking_restores(monkeypatch):
    router, _, guild, voice, mixer = setup_router(mixing=True)
    decoder = PCM([FRAME, FRAME])
    prepared = Prepared(decoder)
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: pytest.fail("decoder duplicado"))
    route = asyncio.create_task(router.play_tts(guild=guild, vc=voice, path=prepared.path, prepared=prepared))
    await asyncio.wait_for(consume_overlay(mixer, route), 2.0)
    result = await route
    assert result["source_primed"]
    assert result["tts_local_ducked"]
    assert result["first_frame_observed"]
    assert result["first_frame_at"] >= result["playback_started_at"]
    assert not mixer.has_overlays
    assert voice.source is mixer
    assert voice.is_connected()
    assert decoder.cleanup_calls == 1
    mixer.cleanup()


@pytest.mark.asyncio
async def test_opus_prepared_source_is_rejected_only_for_pcm_mixer(monkeypatch):
    router, _, guild, voice, mixer = setup_router(mixing=True)
    opus = PCM([b"opus"], opus=True)
    pcm = PCM([FRAME])
    prepared = Prepared(opus)
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: pcm)
    route = asyncio.create_task(router.play_tts(guild=guild, vc=voice, path=prepared.path, prepared=prepared))
    await asyncio.wait_for(consume_overlay(mixer, route), 2.0)
    result = await route
    assert opus.cleanup_calls == 1
    assert result["source_primed"] is False
    assert result["first_frame_observed"]
    assert pcm.cleanup_calls == 1
    mixer.cleanup()


@pytest.mark.asyncio
async def test_mixer_ownership_is_rechecked_after_decoder_preparation(monkeypatch):
    router, state, guild, voice, mixer = setup_router(mixing=True)
    decoder = PCM([FRAME])
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: decoder)
    original = routing.BufferedPCMSource.wait_ready

    async def prepare_and_replace(source, timeout):
        await original(source, timeout)
        state.current_source = None

    monkeypatch.setattr(routing.BufferedPCMSource, "wait_ready", prepare_and_replace)
    with pytest.raises(RuntimeError, match="posse do mixer mudou"):
        await router.play_tts(guild=guild, vc=voice, path="/tmp/tts.mp3")
    assert not mixer.has_overlays
    assert decoder.cleanup_calls == 1
    assert voice.source is mixer
    mixer.cleanup()


@pytest.mark.asyncio
async def test_streaming_is_allowed_only_for_actual_active_local_mixer():
    router, state, _, voice, mixer = setup_router(mixing=True)
    bot = SimpleNamespace(audio_router=router)
    router.is_music_active = lambda guild_id: True
    assert integration.roteador_suporta_preparo_tts(bot)
    assert integration.motivo_bloqueio_streaming_local(bot, 7, voice) is None
    for backend in ("agent", "lavalink"):
        state.current_backend = backend
        assert integration.motivo_bloqueio_streaming_local(bot, 7, voice) == "music_active"
    state.current_backend = "local"
    voice.source = PCM()
    assert integration.motivo_bloqueio_streaming_local(bot, 7, voice) == "music_active"
    voice.source = mixer
    voice.connected = False
    assert integration.motivo_bloqueio_streaming_local(bot, 7, voice) == "music_active"
    mixer.cleanup()


@pytest.mark.asyncio
async def test_legacy_router_does_not_receive_empty_prepared_argument():
    class LegacyRouter:
        async def play_tts(self, *, path):
            return {"path": path}
    bot = SimpleNamespace(audio_router=LegacyRouter())
    assert await integration.tocar_tts_via_roteador(bot, path="tts.mp3") == {"path": "tts.mp3"}


@pytest.mark.asyncio
async def test_closed_mixer_rejects_overlay_instead_of_leaving_pending_future():
    _, _, _, _, mixer = setup_router(mixing=True)
    mixer.cleanup()
    with pytest.raises(RuntimeError, match="encerrou antes da admissão"):
        mixer.add_tts(PCM([FRAME]), volume=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["agent", "lavalink"])
async def test_progressive_fifo_cannot_enter_remote_route_after_ownership_change(monkeypatch, backend):
    router, state, guild, voice, mixer = setup_router(mixing=True)
    assert router.should_allow_local_tts_streaming(7, vc=voice)
    state.current_backend = backend
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: pytest.fail("não deve abrir decoder"))
    with pytest.raises(RuntimeError, match="antes do despacho TTS progressivo"):
        await router.play_tts(guild=guild, vc=voice, path="/tmp/progressive.fifo",
                              stream_error_getter=lambda: None)
    assert voice.source is mixer
    assert voice.is_connected()
    assert not mixer.has_overlays
    mixer.cleanup()


@pytest.mark.asyncio
async def test_cancelling_overlay_releases_decoder_and_preserves_music(monkeypatch):
    router, _, guild, voice, mixer = setup_router(mixing=True)
    decoder = PCM([FRAME] * 100)
    monkeypatch.setattr(routing.discord, "FFmpegPCMAudio", lambda *a, **k: decoder)
    route = asyncio.create_task(router.play_tts(guild=guild, vc=voice, path="/tmp/tts.mp3"))
    async def admitted():
        while not mixer.has_overlays:
            if route.done():
                await route
            await asyncio.sleep(0.001)
    await asyncio.wait_for(admitted(), 2.0)
    route.cancel()
    with pytest.raises(asyncio.CancelledError):
        await route
    assert not mixer.has_overlays
    assert decoder.cleanup_calls == 1
    assert voice.source is mixer
    assert voice.is_playing()
    assert voice.is_connected()
    mixer.cleanup()


@pytest.mark.asyncio
async def test_real_mp3_fifo_starts_local_overlay_before_provider_finishes(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or not hasattr(os, "mkfifo"):
        pytest.skip("FFmpeg e FIFO POSIX necessários")
    generated = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
    ).stdout
    path = tmp_path / "speech.mp3"
    os.mkfifo(path)
    release = threading.Event()
    done = threading.Event()
    errors = []

    def provider():
        try:
            with path.open("wb", buffering=0) as handle:
                split = len(generated) * 3 // 4
                handle.write(generated[:split])
                release.wait(5)
                handle.write(generated[split:])
        except OSError as exc:
            errors.append(exc)
        finally:
            done.set()

    writer = threading.Thread(target=provider, daemon=True)
    writer.start()
    router, _, guild, voice, mixer = setup_router(mixing=True)
    route = asyncio.create_task(router.play_tts(
        guild=guild, vc=voice, path=str(path), timeout=5,
        before_options="-nostdin -f mp3 -probesize 32768 -analyzeduration 0",
    ))
    try:
        async def admitted():
            while not mixer.has_overlays:
                if route.done():
                    await route
                await asyncio.sleep(0.001)
        await asyncio.wait_for(admitted(), 4)
        assert not done.is_set(), "primeiro PCM aguardou todo o áudio"
        assert mixer.read(), "música continua enquanto o provider ainda produz TTS"
        release.set()
        await asyncio.wait_for(consume_overlay(mixer, route), 5)
        result = await route
        assert result["first_frame_observed"]
        assert result["tts_local_ducked"]
        assert voice.source is mixer
        assert not errors
    finally:
        release.set()
        if not route.done():
            route.cancel()
        await asyncio.gather(route, return_exceptions=True)
        mixer.cleanup()
        # Release a writer still waiting for a reader after failed admission.
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            await asyncio.to_thread(writer.join, 2)
        finally:
            os.close(reader)


@pytest.mark.asyncio
async def test_tts_resolve_and_play_activate_fifo_for_music_without_early_decoder(monkeypatch, tmp_path):
    from cogs.tts import audio

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or not hasattr(os, "mkfifo"):
        pytest.skip("FFmpeg e FIFO POSIX necessários")
    generated = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
    ).stdout
    release = threading.Event()
    provider_done = threading.Event()

    class FakeGTTS:
        def __init__(self, **kwargs):
            pass

        def stream(self):
            try:
                split = len(generated) * 3 // 4
                yield generated[:split]
                if not release.wait(5):
                    raise TimeoutError("provider simulado não foi liberado")
                yield generated[split:]
            finally:
                provider_done.set()

    router, _, guild, voice, mixer = setup_router(mixing=True)
    router.is_music_active = lambda guild_id: True
    voice.guild = guild

    class Probe(audio.TTSAudioMixin):
        def __init__(self):
            self.guild_states = {}
            self.bot = SimpleNamespace(audio_router=router, get_guild=lambda _: guild)

        def _get_voice_client_for_guild(self, guild):
            return guild.voice_client

        def _schedule_persistent_synt_success(self, *args):
            pass

    probe = Probe()
    item = audio.QueueItem(guild_id=7, channel_id=9, author_id=3, text="Olá música",
                          engine="gtts", voice="", language="pt", rate="+0%", pitch="+0Hz")
    runtime = tmp_path / "runtime"
    cache = tmp_path / "cache"
    runtime.mkdir()
    cache.mkdir()
    monkeypatch.setattr(audio, "gTTS", FakeGTTS)
    monkeypatch.setattr(audio, "_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(audio, "_CACHE_DIR", str(cache))
    monkeypatch.setattr(audio, "_TTS_REQUIRED_DIRS", (str(runtime), str(cache)))
    monkeypatch.setattr(audio, "TTS_CACHEABLE_TEXT_MAX_LENGTH", 0)
    monkeypatch.setattr(audio, "TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH", 0)
    monkeypatch.setattr(audio, "TTS_GTTS_STREAMING_ENABLED", True)
    monkeypatch.setattr(audio, "TTS_GTTS_STREAM_MIN_CHARS", 1)
    route = None
    try:
        path, cleanup = await probe._resolve_audio_path(probe._get_state(7), item, allow_edge_stream=True)
        handle = probe._edge_stream_handle_for_path(path)
        assert cleanup and handle is not None
        assert not handle.activated, "música deve usar o decoder do mixer"
        assert getattr(handle, "_early_source", None) is None
        route = asyncio.create_task(probe._play_file(voice, path, item=item))

        async def admitted():
            while not mixer.has_overlays:
                if route.done():
                    await route
                await asyncio.sleep(0.001)
        await asyncio.wait_for(admitted(), 4)
        assert handle.activated
        assert not provider_done.is_set(), "TTS aguardou síntese inteira"
        assert mixer.read()
        release.set()
        await asyncio.wait_for(consume_overlay(mixer, route), 5)
        result = await route
        assert result["first_frame_observed"]
        assert result["progressive_stream"]
        assert result["gtts_stream"]
        assert result["tts_local_ducked"]
        assert handle.cleaned
        assert not os.path.exists(path)
        assert voice.source is mixer
        assert not mixer.has_overlays
    finally:
        release.set()
        if route is not None:
            if not route.done():
                route.cancel()
            await asyncio.gather(route, return_exceptions=True)
        for handle in list(probe._get_edge_stream_handles().values()):
            await probe._finalize_edge_stream(handle, cancel=True)
        executor = getattr(probe, "_tts_gtts_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)
        mixer.cleanup()
