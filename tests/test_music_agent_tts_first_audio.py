"""Native first-PCM preparation overlaps voice without leaking its ownership."""
from contextlib import asynccontextmanager
import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def music(monkeypatch):
    from test_tts_helpers import tts_audio
    import discord
    if not hasattr(discord, "AudioSource"):
        monkeypatch.setattr(discord, "AudioSource", type("TestAudioSource", (), {}), raising=False)
    return importlib.import_module("cogs.musica.runtime_telefone.agente.tts")


def test_native_mp3_probe_default_and_explicit_bounds(music, monkeypatch):
    monkeypatch.delenv("TTS_STREAM_FFMPEG_PROBESIZE_BYTES", raising=False)
    assert "-probesize 512" in music._native_stream_input_options()
    for configured, expected in (("2048", 2048), ("-1", 32), ("9999999", 16384), ("bad", 512)):
        monkeypatch.setenv("TTS_STREAM_FFMPEG_PROBESIZE_BYTES", configured)
        assert f"-probesize {expected}" in music._native_stream_input_options()
    assert "-f mp3" in music._native_stream_input_options()
    assert "-analyzeduration 0" in music._native_stream_input_options()


def test_native_reader_keeps_request_budget_below_the_configured_limit(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_TTS_MAX_B64_BYTES", "4096")
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", "/unused/private.sock")
    class Owner(music.TTSMixin):
        def _tts_cache_enabled(self):
            return False
    for requested, expected in ((None, 4096), (128, 128), (999999, 4096)):
        body = {"engine": "gtts", "text": "olá"}
        if requested is not None:
            body["max_audio_bytes"] = requested
        reader = Owner()._native_tts_reader(body, text="olá", engine="gtts", key="")
        try:
            assert reader.max_bytes == expected
            assert music.json.loads(reader.body)["max_audio_bytes"] == expected
        finally:
            reader.close()
    for invalid in (True, 0, -1, "4096", 1.5):
        with pytest.raises(ValueError, match="max_audio_bytes"):
            Owner()._native_tts_reader({"max_audio_bytes": invalid}, text="olá", engine="gtts", key="")


@pytest.mark.asyncio
async def test_native_source_and_cold_voice_are_prepared_concurrently(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    prepared, connecting = asyncio.Event(), asyncio.Event()
    source = SimpleNamespace(closed=False)
    source.cleanup = lambda: setattr(source, "closed", True)
    voice = object()

    class Owner(music.TTSMixin):
        async def _prepare_tts_source(self, body, target, *, started):
            prepared.set()
            await connecting.wait()
            return source, "gtts"
        async def _ensure_direct_voice_client(self, guild):
            connecting.set()
            await asyncio.wait_for(prepared.wait(), 1)
            return voice, True

    async with Owner()._direct_voice_with_prepared_tts(1, {"engine": "gtts", "text": "olá"}, started=time.monotonic()) as result:
        client, created, preparation = result
        assert (client, created) == (voice, True)
        assert await preparation == (source, "gtts")
        assert not source.closed
    assert source.closed


@pytest.mark.asyncio
async def test_failed_voice_reclaims_already_prepared_source(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    prepared = asyncio.Event()
    source = SimpleNamespace(closed=False)
    source.cleanup = lambda: setattr(source, "closed", True)

    class Owner(music.TTSMixin):
        async def _prepare_tts_source(self, *_args, **_kwargs):
            prepared.set()
            return source, "edge"
        async def _ensure_direct_voice_client(self, guild):
            await prepared.wait()
            raise RuntimeError("voice refused")

    with pytest.raises(RuntimeError, match="voice refused"):
        async with Owner()._direct_voice_with_prepared_tts(1, {"engine": "edge"}, started=0):
            pytest.fail("failed connection was admitted to playback")
    assert source.closed


@pytest.mark.asyncio
async def test_failed_voice_cancels_and_joins_pending_preparation(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Owner(music.TTSMixin):
        async def _prepare_tts_source(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        async def _ensure_direct_voice_client(self, guild):
            await started.wait()
            raise RuntimeError("voice refused")

    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="voice refused"):
        async with Owner()._direct_voice_with_prepared_tts(1, {"engine": "gtts"}, started=0):
            pytest.fail("failed connection was admitted to playback")
    assert cancelled.is_set()
    assert not asyncio.all_tasks() - before


@pytest.mark.asyncio
async def test_cancelled_connection_joins_source_preparation(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Owner(music.TTSMixin):
        async def _prepare_tts_source(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        async def _ensure_direct_voice_client(self, guild):
            await asyncio.Future()

    async def command():
        async with Owner()._direct_voice_with_prepared_tts(1, {"engine": "edge"}, started=0):
            pytest.fail("cancelled connection was admitted to playback")
    before = asyncio.all_tasks()
    task = asyncio.create_task(command())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not asyncio.all_tasks() - before


@pytest.mark.asyncio
async def test_prebuilt_and_non_native_paths_do_not_start_speculative_synthesis(music, monkeypatch):
    class Owner(music.TTSMixin):
        async def _prepare_tts_source(self, *_args, **_kwargs):
            pytest.fail("prebuilt/non-native audio started speculative synthesis")
        async def _ensure_direct_voice_client(self, guild):
            return "voice", False
    for native, body in (("1", {"engine": "teto", "audio_b64": "wav"}),
                         ("1", {"engine": "edge", "audio_url": "https://example.invalid/audio"}),
                         ("0", {"engine": "gtts", "text": "olá"})):
        monkeypatch.setenv("TTS_NATIVE_WORKER", native)
        monkeypatch.delenv("TTS_BACKEND", raising=False)
        async with Owner()._direct_voice_with_prepared_tts(1, body, started=0) as result:
            assert result == ("voice", False, None)


@pytest.mark.asyncio
async def test_music_ownership_guard_precedes_native_preparation(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    state = SimpleNamespace(current=object(), player=None, voice_channel_id=0, text_channel_id=0)
    class Owner(music.TTSMixin):
        states = {1: state}
        _tts_direct_locks, _tts_direct_lock_users = {}, {}
        @asynccontextmanager
        async def _registry_lock(self, *_):
            yield
        def _update_auto_leave_from_body(self, *_):
            pass
        async def _ensure_direct_voice_client(self, *_):
            pytest.fail("music ownership guard was bypassed")
        async def _prepare_tts_source(self, *_args, **_kwargs):
            pytest.fail("music ownership guard was bypassed")
    state.public = lambda: {}
    result = await Owner().cmd_voice_tts({"guild_id": 1, "voice_channel_id": 2, "engine": "gtts", "text": "olá"})
    assert result["ok"] is False
    assert "música ativa" in result["error"]


def failing_native_source(music, *, first_frame):
    reader = music._NativeTTSReader({"engine": "edge"}, timeout=2, max_bytes=4096)
    class Decoder:
        closed = False
        reads = 0
        def read(self):
            self.reads += 1
            if first_frame and self.reads == 1:
                return b"\x01" * 3840
            reader.error = RuntimeError("truncated native stream")
            return b""
        def cleanup(self):
            self.closed = True
    decoder = Decoder()
    return music._TimedTTSSource(decoder, started=time.monotonic(), reader=reader), decoder


@pytest.mark.asyncio
@pytest.mark.parametrize("first_frame,timeout", ((True, False), (False, False), (True, True), (False, True)))
async def test_direct_native_failure_is_partial_only_after_real_pcm(music, monkeypatch, first_frame, timeout):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", "/unused/private.sock")
    source, decoder = failing_native_source(music, first_frame=first_frame)
    class Voice:
        playing = False
        stopped = False
        def is_connected(self): return True
        def is_playing(self): return self.playing
        def is_paused(self): return False
        def stop(self):
            self.stopped, self.playing = True, False
        def play(self, audio, after):
            self.playing = True
            try:
                if timeout:
                    if first_frame:
                        assert audio.read()
                    raise TimeoutError("playback timeout")
                audio.read()
                audio.read()
            except Exception as error:
                after(error)
    voice = Voice()
    class Owner(music.TTSMixin):
        states = {}
        _tts_direct_locks, _tts_direct_lock_users = {}, {}
        @asynccontextmanager
        async def _registry_lock(self, *_): yield
        async def _ensure_direct_voice_client(self, *_): return voice, True
        async def _prepare_tts_source(self, *_args, **_kwargs): return source, "edge"
        def _update_auto_leave_from_body(self, *_): pass
        def _cancel_idle_disconnect(self, *_): pass
        def _cancel_voice_presence_disconnect(self, *_): pass
        def _set_voice_session_mode(self, state, mode, **_): state.voice_session_mode = mode
        def _set_status(self, state, status, **_): state.status = status
        async def _finish_tts_voice_session(self, *_args, **_kwargs): pass
        def log(self, *_args, **_kwargs): pass
    command = Owner().cmd_voice_tts({"guild_id": 1, "voice_channel_id": 2, "engine": "edge", "text": "olá"})
    if first_frame:
        result = await command
        assert result["ok"] is False and result["partial_playback"] is True
        assert result["native_stream"] is True and result["first_frame_observed"] is True
        assert result["first_frame_ms"] >= 0
    else:
        with pytest.raises((RuntimeError, TimeoutError)):
            await command
    assert voice.stopped and decoder.closed and source.reader.closed


@pytest.mark.asyncio
async def test_direct_cancellation_after_first_pcm_is_still_cancellation(music, monkeypatch):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", "/unused/private.sock")
    source, decoder = failing_native_source(music, first_frame=True)
    playing = asyncio.Event()
    class Voice:
        stopped = False
        def is_connected(self): return True
        def is_playing(self): return playing.is_set() and not self.stopped
        def is_paused(self): return False
        def stop(self): self.stopped = True
        def play(self, audio, after):
            assert audio.read()
            playing.set()
    voice = Voice()
    class Owner(music.TTSMixin):
        states = {}
        _tts_direct_locks, _tts_direct_lock_users = {}, {}
        @asynccontextmanager
        async def _registry_lock(self, *_): yield
        async def _ensure_direct_voice_client(self, *_): return voice, True
        async def _prepare_tts_source(self, *_args, **_kwargs): return source, "edge"
        def _update_auto_leave_from_body(self, *_): pass
        def _cancel_idle_disconnect(self, *_): pass
        def _cancel_voice_presence_disconnect(self, *_): pass
        def _set_voice_session_mode(self, state, mode, **_): state.voice_session_mode = mode
        def _set_status(self, state, status, **_): state.status = status
        async def _finish_tts_voice_session(self, *_args, **_kwargs): pass
        def log(self, *_args, **_kwargs): pass
    task = asyncio.create_task(Owner().cmd_voice_tts({"guild_id": 1, "voice_channel_id": 2, "engine": "edge", "text": "olá"}))
    await asyncio.wait_for(playing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert voice.stopped and decoder.closed and source.reader.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_route", (True, False))
async def test_overlay_partial_contract_is_limited_to_direct_route(music, monkeypatch, direct_route):
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", "/unused/private.sock")
    source, decoder = failing_native_source(music, first_frame=True)
    class Mixer:
        _closed = False
        reserved = 0
        cancelled = False
        def reserve_tts(self): self.reserved += 1
        def release_tts(self): self.reserved -= 1
        def add_tts(self, audio, **_):
            future = asyncio.get_running_loop().create_future()
            try:
                assert audio.read()
                audio.read()
            except Exception as error:
                future.set_exception(error)
            return future
        def cancel_tts(self, future): self.cancelled = True
        def has_tts(self): return False
    mixer = Mixer()
    player = SimpleNamespace(source=mixer, is_playing=lambda: True)
    state = SimpleNamespace(current=object(), player=player, voice_session_mode="music", ducked=False)
    monkeypatch.setattr(music, "AgentMixedAudioSource", Mixer)
    class Owner(music.TTSMixin):
        states = {1: state}
        async def _prepare_tts_source(self, *_args, **_kwargs): return source, "edge"
        def _update_auto_leave_from_body(self, *_): pass
        async def _refresh_voice_presence_policy(self, *_args, **_kwargs): pass
        def log(self, *_args, **_kwargs): pass
    command = Owner().cmd_tts({"guild_id": 1, "engine": "edge", "text": "olá"}, _allow_direct_fallback=not direct_route)
    if direct_route:
        result = await command
        assert result["ok"] is False and result["partial_playback"] is True
        assert result["native_stream"] is True and result["first_frame_observed"] is True
    else:
        with pytest.raises(RuntimeError, match="progressiva incompleta"):
            await command
    assert mixer.reserved == 0 and mixer.cancelled
    assert decoder.closed and source.reader.closed
