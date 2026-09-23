"""Regressões de latência/qualidade sem depender de rede ou voz do Discord."""
import asyncio
import importlib
import threading
import time
from array import array
from types import SimpleNamespace

import pytest

from .test_music_agent_lifecycle import _load_music_agent


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_TOKEN", "test-token")
    music = _load_music_agent(monkeypatch)
    package = "cogs.musica.runtime_telefone.agente."
    return SimpleNamespace(
        music=music,
        buffer=importlib.import_module(package + "buffer_pcm"),
        preparation=importlib.import_module(package + "preparacao_audio"),
        validity=importlib.import_module(package + "validade_stream"),
        utils=importlib.import_module(package + "utilitarios"),
    )


class PCM:
    def __init__(self, frames=()):
        self.frames = iter(frames)
        self.cleaned = False
        self.reads = 0

    def read(self):
        self.reads += 1
        return next(self.frames, b"")

    def cleanup(self):
        self.cleaned = True


def frame(value=1000):
    return array("h", [value] * 1920).tobytes()


def test_assinatura_valida_evitarenovacao_precoce_mas_respeita_teto(runtime, monkeypatch):
    clock = runtime.validity.time
    monkeypatch.setattr(clock, "time", lambda: 10000.0)
    monkeypatch.setattr(clock, "monotonic", lambda: 1000.0)
    deadline = runtime.validity.prazo_stream
    url = "https://r1.googlevideo.com/videoplayback?expire=14000"
    assert deadline(url, 700, 120) == 2500  # idade local máxima de 1800 s
    assert deadline(url.replace("14000", "10100"), 700, 120) == 1040
    assert deadline(url.replace("14000", "9999"), 999, 120) < 1000
    assert deadline("https://cdn.example/audio?expire=14000", 700, 120) == 820
    assert deadline(url.replace("14000", "NaN"), 700, 120) == 820
    assert deadline("https://evilgooglevideo.com/?expire=14000", 700, 120) == 820


@pytest.mark.parametrize("opus_abr,aac_abr,chosen", [(160, 176, "opus"), (64, 256, "aac")])
def test_selecao_equilibra_codec_e_bitrate(runtime, opus_abr, aac_abr, chosen):
    u = runtime.utils
    formats = [
        {"url": "https://cdn/opus", "acodec": "opus", "abr": opus_abr, "asr": 48000, "vcodec": "none"},
        {"url": "https://cdn/aac", "acodec": "mp4a.40.2", "abr": aac_abr, "asr": 44100, "vcodec": "none"},
    ]
    entry = dict(formats[1], formats=formats)
    result = u.select_playback_stream(entry, format_selector=u.DEFAULT_YTDLP_AUDIO_FORMAT, format_sort=u.DEFAULT_YTDLP_AUDIO_SORT)
    assert result["stream_url"] == "https://cdn/" + chosen
    assert u.select_playback_stream(entry, format_selector="140", format_sort=u.DEFAULT_YTDLP_AUDIO_SORT)["stream_url"] == "https://cdn/aac"


def test_selecao_preserva_idioma_e_exclui_drm(runtime):
    u = runtime.utils
    original = {"url": "https://cdn/pt", "acodec": "aac", "abr": 128, "language": "pt", "vcodec": "none"}
    formats = [original, dict(original, url="https://cdn/en", language="en", abr=400), dict(original, url="https://cdn/drm", has_drm=True, abr=500)]
    result = u.select_playback_stream(dict(original, formats=formats), format_selector=u.DEFAULT_YTDLP_AUDIO_FORMAT, format_sort=u.DEFAULT_YTDLP_AUDIO_SORT)
    assert result["stream_url"] == original["url"]


def test_busca_textual_e_spotify_usam_helper_aquecido(runtime):
    agent = runtime.music.MusicAgent()
    calls = []
    class Hot:
        def resolve(self, target, **kwargs):
            calls.append((target, kwargs))
            return {"stream_url": "https://cdn/audio", "title": "song", "ok": True}
    agent._ytdlp_warm_client = Hot()
    agent._run_ytdlp_process = lambda *a, **k: pytest.fail("fallback frio desnecessário")
    expected = {"title": "song", "author": "artist"}
    result = agent._resolve_with_ytdlp("artist song", expected_metadata=expected)
    assert result["stream_url"] == "https://cdn/audio"
    assert calls[0][0].startswith("ytsearch")
    assert calls[0][1]["expected_metadata"] == expected


def test_promocao_de_prefetch_compartilhado_respeita_ordem(runtime):
    async def scenario():
        agent = runtime.music.MusicAgent()
        agent.resolve_max_concurrency = 1
        await agent._acquire_resolve_slot(0)
        agent._resolve_lock_users["shared"] = 1
        order = []
        async def waiting(key, priority):
            async with agent._resolve_slot(priority, key=key):
                order.append(key)
        other = asyncio.create_task(waiting("other", 5))
        shared = asyncio.create_task(waiting("shared", 20))
        await asyncio.sleep(0)
        await agent._promote_resolution("shared", 0)
        await agent._release_resolve_slot()
        await asyncio.gather(other, shared)
        assert order == ["shared", "other"]
        assert agent._resolve_active == 0
        assert not agent._resolve_waiters and not agent._resolve_waiter_keys and not agent._resolve_running
    asyncio.run(scenario())


def test_play_preempta_background_diferente_mas_nao_o_compartilhado(runtime):
    async def scenario():
        agent = runtime.music.MusicAgent()
        agent.resolve_max_concurrency = 1
        entered = asyncio.Event()
        async def background():
            async with agent._resolve_slot(10, key="song"):
                entered.set()
                await asyncio.Event().wait()
        agent._resolve_priorities["song"] = 10
        agent._resolve_lock_users["song"] = 1
        task = asyncio.create_task(background())
        await entered.wait()
        await agent._promote_resolution("song", 0)
        assert not task.cancelling()
        await agent._promote_resolution("different", 0)
        assert not task.cancelling()  # compartilhado já tem consumidor interativo
        agent._resolve_priorities["song"] = 10
        await agent._promote_resolution("different", 0)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent._resolve_active == 0 and not agent._resolve_running
    asyncio.run(scenario())


def test_buffer_aplica_backpressure_preserva_frames_e_distingue_eof(runtime):
    async def scenario():
        raw = PCM([frame(1), frame(2), frame(3)])
        buffered = runtime.buffer.BufferedPCMSource(raw, max_frames=2)
        try:
            await buffered.wait_ready(timeout=1, min_frames=2)
            assert raw.reads == 2
            assert buffered.audio_buffer_metrics()["buffer_max_bytes"] == 7680
            assert buffered.read() == frame(1)
            assert buffered.read() == frame(2)
            await buffered.wait_ready(timeout=1)
            assert buffered.read() == frame(3)
            for _ in range(100):
                if buffered._eof:
                    break
                await asyncio.sleep(0.001)
            assert buffered.read() == b""
            assert buffered.audio_buffer_metrics()["buffer_peak_frames"] <= 2
        finally:
            buffered.cleanup()
        assert raw.cleaned
    asyncio.run(scenario())


def test_tts_continua_com_decoder_bloqueado_sem_confirmar_audio_falso(runtime):
    async def scenario():
        gate = threading.Event()
        class Blocked(PCM):
            def read(self):
                gate.wait(2)
                return b""
            def cleanup(self):
                super().cleanup()
                gate.set()
        raw = Blocked()
        buffered = runtime.buffer.BufferedPCMSource(raw, stall_seconds=0.1)
        mixer = runtime.music.AgentMixedAudioSource(loop=asyncio.get_running_loop(), music_source=buffered, music_volume=1)
        done = mixer.add_tts(PCM([frame(5000)]))
        try:
            started = time.monotonic()
            output = mixer.read()
            assert time.monotonic() - started < 0.1
            assert array("h", output)[0] == 5000
            assert mixer.first_frame_ms is None
            assert mixer.audio_telemetry()["buffer_underruns"] == 1
            buffered._empty_since = time.monotonic() - 1
            with pytest.raises(TimeoutError):
                buffered.read()
            assert not done.done()
        finally:
            mixer.cleanup()
            await asyncio.sleep(0)
        assert raw.cleaned
    asyncio.run(scenario())


def test_buffer_falha_inicial_e_cancelamento_liberam_decoder(runtime):
    async def scenario():
        agent = runtime.music.MusicAgent()
        raw = PCM([])
        source = runtime.buffer.BufferedPCMSource(raw)
        agent._create_pcm_source = lambda track: source
        with pytest.raises(RuntimeError, match="sem produzir"):
            await agent._prepare_current_pcm(1, runtime.music.AgentTrack(), 0)
        assert raw.cleaned and not agent._starting_pcm
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["queue_clear", "pause", "stop", "different_item", "expired"])
def test_audio_preparado_e_descartado_quando_invalido(runtime, change):
    async def scenario():
        m = runtime.music
        agent = m.MusicAgent()
        track = m.AgentTrack(stream_url="https://cdn/next")
        raw = PCM([frame()] * 5)
        source = runtime.buffer.BufferedPCMSource(raw)
        await source.wait_ready(timeout=1)
        entry = runtime.preparation.AudioPreparado(track.queue_item_id, track.stream_url, 0, source, time.monotonic(), True)
        agent._prepared_audio[1] = entry
        state = m.GuildMusicState(guild_id=1, current=m.AgentTrack(duration=100), queue=[track], started_monotonic=time.monotonic())
        agent.states[1] = state
        if change == "queue_clear":
            await agent.cmd_queue_clear({"guild_id": 1})
        elif change == "pause":
            state.paused = True
            agent._schedule_audio_prepare(1)
        elif change == "stop":
            agent._bump_playback_generation(state, reason="stop")
        elif change == "different_item":
            assert agent._take_prepared_audio(1, m.AgentTrack(stream_url=track.stream_url)) is None
        else:
            entry.created_at -= 46
            assert agent._take_prepared_audio(1, track) is None
        assert raw.cleaned and not agent._prepared_audio
    asyncio.run(scenario())


def test_proxima_faixa_reutiliza_pcm_e_limita_ffmpeg_adicional(runtime):
    async def scenario():
        m = runtime.music
        agent = m.MusicAgent()
        created = []
        def create(track):
            source = runtime.buffer.BufferedPCMSource(PCM([frame()] * 20))
            created.append(source)
            return source
        agent._create_pcm_source = create
        for gid in (1, 2):
            track = m.AgentTrack(stream_url="https://cdn/next", stream_resolved_monotonic=time.monotonic())
            agent.states[gid] = m.GuildMusicState(guild_id=gid, current=m.AgentTrack(duration=5), queue=[track], started_monotonic=time.monotonic())
            agent._schedule_audio_prepare(gid)
        await asyncio.gather(*list(agent._audio_prepare_tasks.values()))
        assert len(created) == 1 and len(agent._prepared_audio) == 1
        gid = next(iter(agent._prepared_audio))
        track = agent.states[gid].queue[0]
        taken = await agent._prepare_current_pcm(gid, track, 0)
        assert taken is created[0] and len(created) == 1
        assert not agent._prepared_audio and not agent._audio_prepare_tasks
        taken.cleanup()
    asyncio.run(scenario())


def test_confirmacao_exige_primeiro_frame_real(runtime):
    async def scenario():
        agent = runtime.music.MusicAgent()
        voice = SimpleNamespace(is_connected=lambda: True, is_playing=lambda: True, is_paused=lambda: False)
        source = SimpleNamespace(first_frame_ms=None)
        with pytest.raises(TimeoutError, match="primeiro frame"):
            await agent._confirm_direct_playback(voice, source, max_delay=0.05)
        source.first_frame_ms = 0.0
        assert await agent._confirm_direct_playback(voice, source, max_delay=1) < 0.05
    asyncio.run(scenario())


def test_mistura_tem_headroom_e_ducking_gradual(runtime):
    async def scenario():
        mixer = runtime.music.AgentMixedAudioSource(loop=asyncio.get_running_loop(), music_source=PCM([frame(30000)] * 30), music_volume=1, duck_factor=0.08)
        done = mixer.add_tts(PCM([frame(30000)] * 3))
        outputs = [mixer.read() for _ in range(4)]
        assert all(max(array("h", output)) <= 32002 for output in outputs)
        assert mixer.audio_telemetry()["mix_limited_frames"] > 0
        await asyncio.sleep(0)
        assert done.done()
        release = [array("h", mixer.read())[-1] for _ in range(8)]
        assert release == sorted(release)
        assert release[0] < release[-1] < 30000
        mixer.cleanup()
    asyncio.run(scenario())


def test_bitrate_muda_na_thread_de_audio_sem_recriar_sessao(runtime):
    async def scenario():
        m = runtime.music
        agent = m.MusicAgent()
        agent._loop = asyncio.get_running_loop()
        agent._schedule_next_queue_prefetch = lambda *a, **k: None
        calls = []
        class Encoder:
            def set_bitrate(self, value):
                calls.append((value, threading.get_ident()))
                return value
            def set_signal_type(self, value): assert value == "music"
            def set_bandwidth(self, value): assert value == "full"
        class Voice:
            channel = SimpleNamespace(id=9, bitrate=256000)
            encoder = Encoder()
            source = None
            plays = 0
            def is_connected(self): return True
            def is_playing(self): return self.source is not None
            def is_paused(self): return False
            def play(self, source, **kwargs):
                self.source = source
                self.plays += 1
                self.initial = kwargs["bitrate"]
        voice = Voice()
        agent._create_pcm_source = lambda track: runtime.buffer.BufferedPCMSource(PCM([frame()] * 10))
        async def confirm(voice, source, **kwargs):
            await asyncio.to_thread(source.read)
            return 0
        agent._confirm_direct_playback = confirm
        first = m.AgentTrack(stream_url="https://cdn/one", audio_abr=96)
        state = m.GuildMusicState(guild_id=1, voice_channel_id=9, current=first)
        agent.states[1] = state
        await agent._play_direct_voice(1, first, prepared_voice=(voice, False))
        assert voice.initial == 128
        mixer = voice.source
        mixer.stop_music()
        second = m.AgentTrack(stream_url="https://cdn/two", audio_abr=256)
        state.current = second
        await agent._play_direct_voice(1, second, prepared_voice=(voice, False))
        assert voice.plays == 1 and voice.source is mixer
        assert calls[0][0] == 256 and calls[0][1] != threading.get_ident()
        assert mixer.quality_context["opus_bitrate_kbps"] == 256
        mixer.cleanup()
    asyncio.run(scenario())


def test_reconexao_eof_apenas_live(runtime, monkeypatch):
    agent = runtime.music.MusicAgent()
    agent.pcm_buffer_enabled = False
    options = []
    def pcm(url, **kwargs):
        options.append(kwargs["before_options"])
        return PCM()
    monkeypatch.setattr(runtime.music.discord, "FFmpegPCMAudio", pcm)
    agent._create_pcm_source(runtime.music.AgentTrack(stream_url="https://cdn/music"))
    agent._create_pcm_source(runtime.music.AgentTrack(stream_url="https://cdn/live", is_live=True))
    assert "-reconnect_at_eof" not in options[0]
    assert "-reconnect_at_eof 1" in options[1]
    assert "403" not in options[0] and "429" not in options[0]


def test_seek_durante_preparo_nao_deixa_erro_antigo_afetar_nova_reproducao(runtime):
    async def scenario():
        m = runtime.music
        agent = m.MusicAgent()
        gate = threading.Event()
        class Blocked(PCM):
            def read(self):
                gate.wait(2)
                return b""
            def cleanup(self):
                super().cleanup()
                gate.set()
        raw = Blocked()
        agent._create_pcm_source = lambda track: runtime.buffer.BufferedPCMSource(raw)
        track = m.AgentTrack(stream_url="https://cdn/one")
        state = m.GuildMusicState(guild_id=1, voice_channel_id=9, current=track)
        agent.states[1] = state
        voice = SimpleNamespace(channel=SimpleNamespace(id=9, bitrate=128000), source=None,
                                is_connected=lambda: True, is_playing=lambda: False, is_paused=lambda: False,
                                play=lambda *a, **k: pytest.fail("fonte antiga não pode tocar"))
        pending = asyncio.create_task(agent._play_direct_voice(1, track, prepared_voice=(voice, False)))
        await asyncio.sleep(0)
        assert 1 in agent._starting_pcm
        agent._bump_playback_generation(state, reason="seek")
        state.status = "playing"
        await asyncio.wait_for(pending, 1)
        assert raw.cleaned and not agent._starting_pcm
        assert state.current is track and state.status == "playing"
    asyncio.run(scenario())


def test_reacao_lenta_nao_bloqueia_inicio_e_finish_remove_apos_add(monkeypatch):
    from cogs.musica.interface.carregamento import MusicLoadingReaction
    async def scenario():
        gate = asyncio.Event()
        added = asyncio.Event()
        actions = []
        class Message:
            _state = SimpleNamespace(user=object())
            async def add_reaction(self, emoji):
                added.set()
                await gate.wait()
                actions.append("add")
            async def remove_reaction(self, emoji, user): actions.append("remove")
        reaction = MusicLoadingReaction(Message(), emoji="⌛")
        reaction.start_background()
        await added.wait()
        assert not reaction.active and not actions
        finishing = asyncio.create_task(reaction.finish())
        await asyncio.sleep(0)
        assert not finishing.done()
        gate.set()
        await finishing
        assert actions == ["add", "remove"] and not reaction.active
    asyncio.run(scenario())
