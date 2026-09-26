from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent
from cogs.musica.testes.test_sincronizacao_worker import _RouterSyncFalso
from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente
from cogs.musica.reproducao import controle_remoto


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg ausente")
def test_filtro_nightcore_com_bassboost_preserva_saida_48k_e_acelera(tmp_path: Path, monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    options, _mode = agent._ffmpeg_options_for_source(44100, effects=(True, True))
    assert options.count("-af ") == 1
    assert "bass=" not in options and "alimiter=" not in options
    audio = tmp_path / "effect.wav"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
        "-af", options.split("-af ", 1)[1], "-ar", "48000", str(audio),
    ], check=True, timeout=10)
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=sample_rate",
        "-of", "default=noprint_wrappers=1", str(audio),
    ], text=True, timeout=5)
    assert "sample_rate=48000" in probe
    duration = float(next(line.split("=", 1)[1] for line in probe.splitlines() if line.startswith("duration=")))
    assert 0.79 <= duration <= 0.81
    with wave.open(str(audio), "rb") as decoded:
        samples = array("h", decoded.readframes(decoded.getnframes()))
        channels = decoded.getnchannels()
    crossings = sum(samples[index - channels] <= 0 < samples[index]
                    for index in range(channels, len(samples), channels))
    assert 540 <= crossings / duration <= 560  # 440 Hz sobe para ~550 Hz
    agent.ffmpeg_options = "-vn -af volume=0.8"
    assert agent._ffmpeg_options_for_source(48000, effects=(True, False))[0] == agent.ffmpeg_options
    with pytest.raises(ValueError, match="personalizado"):
        agent._ffmpeg_options_for_source(48000, effects=(False, True))
    agent.ffmpeg_options = "-vn -sn -dn -loglevel warning"
    assert agent._ffmpeg_options_for_source(48000, effects=(False, True), is_live=True)[0] == agent.ffmpeg_options


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg ausente")
def test_nightcore_rejeita_aliasing_sem_filtrar_musica_nativa(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    clean, mode = agent._ffmpeg_options_for_source(48000)
    assert clean == agent.ffmpeg_options and mode == "native_48k"
    options, _ = agent._ffmpeg_options_for_source(48000, effects=(False, True))
    assert options.count("-af ") == 1
    assert "asetrate=60000,aresample=48000:resampler=swr:filter_size=64" in options

    def alias_amplitude(chain: str) -> float:
        pcm = subprocess.check_output([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*20000*t):s=48000:d=1",
            "-af", chain, "-ar", "48000", "-ac", "1", "-f", "f32le", "-",
        ], timeout=10)
        samples = array("f", pcm)
        window = samples[4800:33600]
        sine = sum(value * math.sin(2 * math.pi * 23000 * i / 48000)
                   for i, value in enumerate(window))
        cosine = sum(value * math.cos(2 * math.pi * 23000 * i / 48000)
                     for i, value in enumerate(window))
        return 2 * math.hypot(sine, cosine) / len(window)

    original = alias_amplitude("aresample=48000,asetrate=60000,aresample=48000")
    improved = alias_amplitude(options.split("-af ", 1)[1])
    assert improved < original * 0.1

    agent.resample_filter_size = 32  # configuração antiga continua no ambiente
    legacy, _ = agent._ffmpeg_options_for_source(48000, effects=(False, True))
    assert ":filter_size=64" in legacy
    assert alias_amplitude(legacy.split("-af ", 1)[1]) < original * 0.1

    agent.resample_quality_enabled = False
    fallback, _ = agent._ffmpeg_options_for_source(48000, effects=(False, True))
    assert fallback.endswith("-af aresample=48000,asetrate=60000,aresample=48000")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg ausente")
def test_slowed_reverb_abaixa_tom_e_deixa_cauda_sem_clipping(tmp_path: Path, monkeypatch) -> None:
    agent = _load_music_agent(monkeypatch).MusicAgent()
    options, _mode = agent._ffmpeg_options_for_source(44100, effects=(True, False, True))
    chain = options.split("-af ", 1)[1]
    assert options.count("-af ") == 1
    assert "asetrate=38400" in chain and "aecho=" in chain and "alimiter=" in chain
    assert "bass=" not in chain  # o grave fica no mixer PCM, depois do decoder
    audio = tmp_path / "slowed.wav"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
        "-af", chain, "-ar", "48000", str(audio),
    ], check=True, timeout=10)
    with wave.open(str(audio), "rb") as decoded:
        assert decoded.getframerate() == 48000
        assert decoded.getnframes() / 48000 > 1.7  # 1,25 s mais a cauda da ambiência
        assert decoded.getnchannels() == 1
        samples = array("h", decoded.readframes(decoded.getnframes()))
    region = samples[48000 // 4 : 48000]
    crossings = sum(region[i - 1] <= 0 < region[i] for i in range(1, len(region)))
    assert 342 < crossings / 0.75 < 362  # 440 Hz cai para ~352 Hz
    assert max(abs(sample) for sample in samples[round(1.45 * 48000) : round(1.65 * 48000)]) > 100
    assert max(map(abs, samples)) <= 32000


def test_bassboost_no_mixer_reforca_graves_apos_volume_sem_reduzir_medios(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    rate = 48000

    async def render(bass_level: float, mid_level: float, enabled: bool, bass_frequency: int = 80) -> array:
        samples = array("h")
        for i in range(rate):
            value = int(32767 * (bass_level * math.sin(2 * math.pi * bass_frequency * i / rate)
                                 + mid_level * math.sin(2 * math.pi * 1000 * i / rate)))
            samples.extend((value, value))

        class Source(music.discord.AudioSource):
            def __init__(self): self.index = 0
            def read(self):
                start = self.index * 1920
                self.index += 1
                return samples[start : start + 1920].tobytes()
            def cleanup(self): pass

        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(), music_source=Source(), music_volume=0.55,
            persistent=True, bassboost=enabled,
        )
        try:
            result = array("h")
            for _ in range(50):
                result.frombytes(mixer.read())
            return result
        finally:
            mixer.cleanup()

    def amplitude(samples: array, frequency: int) -> float:
        start, end = rate // 4, 3 * rate // 4
        window = samples[start * 2 : end * 2 : 2]
        sine = sum(value * math.sin(2 * math.pi * frequency * (i + start) / rate)
                   for i, value in enumerate(window))
        cosine = sum(value * math.cos(2 * math.pi * frequency * (i + start) / rate)
                     for i, value in enumerate(window))
        return 2 * math.hypot(sine, cosine) / len(window) / 32767

    async def scenario() -> None:
        normal = await render(0.10, 0.10, False)
        boosted = await render(0.10, 0.10, True)
        assert amplitude(boosted, 80) > amplitude(normal, 80) * 4.2
        assert 0.95 <= amplitude(boosted, 1000) / amplitude(normal, 1000) <= 1.12
        midbass_normal = await render(0.10, 0.10, False, bass_frequency=160)
        midbass_boosted = await render(0.10, 0.10, True, bass_frequency=160)
        assert amplitude(midbass_boosted, 160) > amplitude(midbass_normal, 160) * 3.1
        inaudible_normal = await render(0.10, 0.10, False, bass_frequency=12)
        inaudible_boosted = await render(0.10, 0.10, True, bass_frequency=12)
        assert (amplitude(inaudible_boosted, 12) / amplitude(inaudible_normal, 12)
                < amplitude(boosted, 80) / amplitude(normal, 80) * 0.65)
        loud_normal = await render(0.39, 0.36, False)
        loud = await render(0.39, 0.36, True)
        assert amplitude(loud, 80) >= amplitude(loud_normal, 80) * 3.2
        assert amplitude(loud, 1000) >= amplitude(loud_normal, 1000) * 0.95
        assert sum(value * value for value in loud) >= sum(value * value for value in loud_normal)
        assert max(map(abs, loud)) <= 32000

    asyncio.run(scenario())


def test_bassboost_nao_some_por_picos_de_faixa_masterizada(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    rate = 48000
    song = array("h")
    for i in range(rate):
        value = (0.25 * math.sin(2 * math.pi * 80 * i / rate)
                 + 0.4 * math.sin(2 * math.pi * 1000 * i / rate)
                 + 0.6 * math.sin(2 * math.pi * 6500 * i / rate))
        if i % 960 in range(142, 150):
            value += 0.6  # um pico curto em cada quadro de 20 ms
        value = max(-1.0, min(1.0, value))
        song.extend((round(32767 * value), round(32767 * value)))

    async def render(enabled: bool, volume: float) -> array:
        class Frames(music.discord.AudioSource):
            def __init__(self): self.position = 0
            def read(self):
                start = self.position * 1920
                self.position += 1
                return song[start:start + 1920].tobytes()
            def cleanup(self): pass

        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(), music_source=Frames(),
            music_volume=volume, persistent=True, bassboost=enabled,
        )
        output = array("h")
        try:
            for _ in range(50): output.frombytes(mixer.read())
        finally:
            mixer.cleanup()
        return output

    def bass_amplitude(samples: array) -> float:
        segment = samples[rate // 2 : 3 * rate // 2 : 2]
        return abs(sum(value * complex(
            math.cos(2 * math.pi * 80 * index / rate),
            math.sin(2 * math.pi * 80 * index / rate),
        ) for index, value in enumerate(segment)))

    async def scenario() -> None:
        for volume, minimum in ((0.55, 3.8), (0.8, 2.0)):
            dry = await render(False, volume)
            boosted = await render(True, volume)
            assert bass_amplitude(boosted) / bass_amplitude(dry) > minimum
            assert max(map(abs, boosted)) <= 32767

    asyncio.run(scenario())


def test_bassboost_nao_vaza_para_outro_canal_nem_altera_tts(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    rate = 48000
    song = array("h")
    voice = array("h")
    for index in range(rate):
        bass = int(32767 * 0.20 * math.sin(2 * math.pi * 80 * index / rate))
        speech = int(32767 * 0.12 * math.sin(2 * math.pi * 1000 * index / rate))
        song.extend((bass, 0))
        voice.extend((speech, speech))

    async def render(enabled: bool) -> array:
        class Frames(music.discord.AudioSource):
            def __init__(self, data): self.data, self.position = data, 0
            def read(self):
                start = self.position * 1920
                self.position += 1
                return self.data[start:start + 1920].tobytes()
            def cleanup(self): pass

        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(), music_source=Frames(song),
            music_volume=0.55, persistent=True, bassboost=enabled,
        )
        mixer.add_tts(Frames(voice), volume=1.0)
        output = array("h")
        try:
            for _ in range(50):
                output.frombytes(mixer.read())
        finally:
            mixer.cleanup()
        return output

    def amplitude(samples: array, frequency: int, channel: int) -> float:
        start, end = rate // 4, 3 * rate // 4
        window = samples[start * 2 + channel : end * 2 + channel : 2]
        sine = sum(value * math.sin(2 * math.pi * frequency * (i + start) / rate)
                   for i, value in enumerate(window))
        cosine = sum(value * math.cos(2 * math.pi * frequency * (i + start) / rate)
                     for i, value in enumerate(window))
        return 2 * math.hypot(sine, cosine) / len(window) / 32767

    async def scenario() -> None:
        normal, boosted = await render(False), await render(True)
        assert amplitude(boosted, 80, 0) > amplitude(normal, 80, 0) * 3.5
        assert amplitude(boosted, 80, 1) < 0.001
        assert 0.97 <= amplitude(boosted, 1000, 0) / amplitude(normal, 1000, 0) <= 1.03
        assert 0.97 <= amplitude(boosted, 1000, 1) / amplitude(normal, 1000, 1) <= 1.03

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_troca_efeitos_preserva_mixer_fila_e_ponto_da_faixa(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    from cogs.musica.runtime_telefone.agente.buffer_pcm import BufferedPCMSource

    agent = music.MusicAgent()
    agent._loop = asyncio.get_running_loop()
    gid = 310
    emitted = []

    class PCM(music.discord.AudioSource):
        def __init__(self):
            self.cleaned = False

        def read(self):
            return b"\x01\x00" * 1920

        def cleanup(self):
            self.cleaned = True

    initial = PCM()
    mixer = music.AgentMixedAudioSource(loop=agent._loop, music_source=initial,
                                        music_volume=0.55, persistent=True)

    class Voice:
        source = mixer

        def is_playing(self):
            return True

        def is_paused(self):
            return False

    current = music.AgentTrack(title="atual", stream_url="https://cdn.invalid/track", duration=120)
    queued = music.AgentTrack(title="próxima", query="next")
    st = music.GuildMusicState(guild_id=gid, current=current, queue=[queued],
                               player=Voice(), status="playing", transport="direct",
                               started_monotonic=time.monotonic() - 10)
    agent.states[gid] = st

    def create(_track, *, effects=(False, False, False)):
        emitted.append((effects, PCM()))
        return BufferedPCMSource(emitted[-1][1], max_frames=10)

    agent._create_pcm_source = create
    agent.prefetch_enabled = False
    try:
        first = await agent._dispatch_action({"guild_id": gid, "effect": "nightcore", "enabled": True,
                                               "expected_revision": 0}, "audio_effect")
        assert first["ok"] and st.playback_speed == 1.25
        assert 9.5 <= st.current.start_offset_seconds <= 11.0
        assert st.queue == [queued] and st.current is current and st.player.source is mixer
        assert initial.cleaned and emitted[0][0] == (False, True, False)

        st.paused = True
        st.status = "paused"
        st.paused_monotonic = time.monotonic()
        st.player.is_playing = lambda: False
        st.player.is_paused = lambda: True
        paused_position = st.source_position_seconds()
        second = await agent.cmd_audio_effect({"guild_id": gid, "effect": "bassboost", "enabled": True,
                                               "expected_revision": 1})
        assert second["ok"] and st.bassboost and st.nightcore
        assert mixer.bassboost_enabled and len(emitted) == 1 and not emitted[0][1].cleaned
        assert st.playback_token == 1  # o decoder e o relógio seguem intactos
        assert st.queue == [queued] and st.paused and st.status == "paused"
        assert abs(st.source_position_seconds(now=st.paused_monotonic + 10) - paused_position) < 0.1

        stale = await agent.cmd_audio_effect({"guild_id": gid, "effect": "nightcore", "enabled": False,
                                              "expected_revision": 0})
        assert not stale["ok"] and len(emitted) == 1 and st.nightcore
        assert st.source_position_seconds() >= st.current.start_offset_seconds

        slowed = await agent.cmd_audio_effect({"guild_id": gid, "effect": "slowed_reverb", "enabled": True,
                                               "expected_revision": 2})
        assert slowed["ok"] and st.slowed_reverb and not st.nightcore
        assert st.playback_speed == 0.8 and st.paused and st.queue == [queued]
        assert len(emitted) == 2 and emitted[-1][0] == (True, False, True)
        assert emitted[0][1].cleaned and st.player.source is mixer

        nightcore = await agent.cmd_audio_effect({"guild_id": gid, "effect": "nightcore", "enabled": True,
                                                  "expected_revision": 3})
        assert nightcore["ok"] and st.nightcore and not st.slowed_reverb
        assert st.playback_speed == 1.25 and emitted[-1][0] == (True, True, False)

        plain = await agent.cmd_audio_effect({"guild_id": gid, "effect": "nightcore", "enabled": False,
                                              "expected_revision": 4})
        assert plain["ok"] and st.playback_speed == 1.0
        assert not st.nightcore and not st.slowed_reverb
        assert emitted[-1][0] == (True, False, False)
    finally:
        mixer.cleanup()


@pytest.mark.asyncio
async def test_bassboost_altera_transmissao_ao_vivo_sem_reiniciar_decoder(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    agent._loop = asyncio.get_running_loop()

    class PCM(music.discord.AudioSource):
        def read(self): return b"\x01\x00" * 1920
        def cleanup(self): pass

    stream = PCM()
    mixer = music.AgentMixedAudioSource(loop=agent._loop, music_source=stream,
                                        music_volume=0.55, persistent=True)

    class Voice:
        source = mixer
        def is_playing(self): return True
        def is_paused(self): return False

    st = music.GuildMusicState(guild_id=99, current=music.AgentTrack(stream_url="http://live", is_live=True),
                               player=Voice(), status="playing", transport="direct",
                               started_monotonic=time.monotonic() - 8, playback_token=5)
    agent.states[99] = st
    agent._create_pcm_source = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("decoder reiniciado"))
    try:
        response = await agent.cmd_audio_effect({"guild_id": 99, "effect": "bassboost", "enabled": True,
                                                 "expected_revision": 0})
        assert response["ok"] and st.bassboost and mixer.bassboost_enabled
        assert st.player.source is mixer and mixer.music_source is stream and st.playback_token == 5
        assert st.current.start_offset_seconds == 0
        rejected = await agent.cmd_audio_effect({"guild_id": 99, "effect": "slowed_reverb", "enabled": True,
                                                 "expected_revision": 1})
        assert not rejected["ok"] and not st.slowed_reverb and st.playback_token == 5
    finally:
        mixer.cleanup()


@pytest.mark.asyncio
async def test_falha_do_novo_decoder_deixa_audio_antigo_tocando(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    from cogs.musica.runtime_telefone.agente.buffer_pcm import BufferedPCMSource

    agent = music.MusicAgent()
    agent._loop = asyncio.get_running_loop()

    class PCM(music.discord.AudioSource):
        def __init__(self, valid=True):
            self.valid = valid
            self.cleaned = False

        def read(self):
            return b"\x01\x00" * 1920 if self.valid else b""

        def cleanup(self):
            self.cleaned = True

    old = PCM()
    mixer = music.AgentMixedAudioSource(loop=agent._loop, music_source=old, music_volume=0.55, persistent=True)
    class Voice:
        source = mixer
        def is_playing(self): return True
        def is_paused(self): return False
    st = music.GuildMusicState(guild_id=11, current=music.AgentTrack(stream_url="http://test"),
                               player=Voice(), transport="direct", status="playing",
                               started_monotonic=time.monotonic())
    agent.states[11] = st
    failed = PCM(valid=False)
    agent._create_pcm_source = lambda _track, **kw: BufferedPCMSource(failed, max_frames=10)
    try:
        result = await agent.cmd_audio_effect({"guild_id": 11, "effect": "nightcore", "enabled": True,
                                                "expected_revision": 0})
        assert not result["ok"] and not st.nightcore and st.playback_token == 0
        assert mixer.music_source is old and not old.cleaned and failed.cleaned
    finally:
        mixer.cleanup()


@pytest.mark.asyncio
async def test_snapshot_do_worker_atualiza_painel_e_relogio_sem_nova_faixa() -> None:
    router = _RouterSyncFalso()
    current = {"title": "faixa", "webpage_url": "https://example.invalid/faixa", "duration": 100}
    base = {"status": "playing", "confirmed_playing": True, "voice_connected": True,
            "player_present": True, "current": current}
    await sincronizar_estado_agente(router, 42, agent_state={**base, "playback_token": 1,
                                                             "position_ms": 10000}, create_panel=False)
    await sincronizar_estado_agente(router, 42, agent_state={**base, "playback_token": 2,
                                                             "last_event": "audio_effect", "position_ms": 22500,
                                                             "bassboost": True, "nightcore": True,
                                                             "speed_multiplier": 1.25, "effects_revision": 1},
                                    create_panel=False)
    st = router.state
    assert st.bassboost and st.nightcore and st.playback_speed == 1.25
    assert st.agent_started_playback_token == 2
    assert 22.5 <= st.current_start_offset_seconds < 23

    await sincronizar_estado_agente(router, 42, agent_state={**base, "playback_token": 3,
                                                             "last_event": "audio_effect", "position_ms": 24000,
                                                             "bassboost": True, "nightcore": False,
                                                             "slowed_reverb": True, "speed_multiplier": 0.8,
                                                             "effects_revision": 2}, create_panel=False)
    assert st.slowed_reverb and not st.nightcore and st.playback_speed == 0.8


@pytest.mark.asyncio
async def test_fonte_preparada_com_modo_antigo_eh_descartada(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    from cogs.musica.runtime_telefone.agente.buffer_pcm import BufferedPCMSource
    from cogs.musica.runtime_telefone.agente.preparacao_audio import AudioPreparado

    class PCM(music.discord.AudioSource):
        def __init__(self): self.cleaned = False
        def read(self): return b"\x01\x00" * 1920
        def cleanup(self): self.cleaned = True

    agent = music.MusicAgent()
    track = music.AgentTrack(stream_url="https://cdn.invalid/next")
    st = music.GuildMusicState(guild_id=51, current=music.AgentTrack(title="atual"),
                               queue=[track], nightcore=True)
    agent.states[51] = st
    raw = PCM()
    source = BufferedPCMSource(raw, max_frames=2)
    await source.wait_ready(timeout=1)
    agent._prepared_audio[51] = AudioPreparado(track.queue_item_id, track.stream_url, 0,
                                                source, time.monotonic(), True, (False, False, False))
    assert agent._take_prepared_audio(51, track) is None
    assert raw.cleaned
    st.nightcore = False
    st.bassboost = True
    keep = PCM()
    prepared = BufferedPCMSource(keep, max_frames=2)
    await prepared.wait_ready(timeout=1)
    agent._prepared_audio[51] = AudioPreparado(track.queue_item_id, track.stream_url, 0,
                                                prepared, time.monotonic(), True, (False, False, False))
    assert agent._take_prepared_audio(51, track) is prepared
    assert not keep.cleaned
    prepared.cleanup()
    st.slowed_reverb = True
    stale = PCM()
    stale_prepared = BufferedPCMSource(stale, max_frames=2)
    await stale_prepared.wait_ready(timeout=1)
    agent._prepared_audio[51] = AudioPreparado(track.queue_item_id, track.stream_url, 0,
                                                stale_prepared, time.monotonic(), True, (False, False, False))
    assert agent._take_prepared_audio(51, track) is None
    assert stale.cleaned


def test_relogio_nightcore_congela_pausado_e_recupera_no_tempo_da_fonte(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    track = music.AgentTrack(duration=100, start_offset_seconds=20)
    st = music.GuildMusicState(guild_id=1, current=track, status="paused", paused=True,
                               started_monotonic=10, paused_monotonic=14, nightcore=True)
    assert st.source_position_seconds(now=999) == 25.0
    assert agent._resume_offset_for_track(track, played_for=4, speed=st.playback_speed) == 24.65


@pytest.mark.asyncio
async def test_acao_vps_envia_estado_explicito_e_sincroniza_resposta(monkeypatch) -> None:
    calls = []
    synced = []

    async def command(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True, "state": {"status": "playing", "bassboost": True,
                                       "nightcore": False, "effects_revision": 2}}

    async def sync(guild_id, fallback, remote, **_kwargs):
        synced.append((guild_id, fallback, remote))

    monkeypatch.setattr(controle_remoto, "music_agent_command", command)
    result = await controle_remoto.ajustar_efeito(
        SimpleNamespace(sync_music_agent_state=sync), 123, "bassboost", True, expected_revision=1,
    )
    assert result["ok"] and calls[0][0] == "audio_effect"
    assert calls[0][1]["effect"] == "bassboost" and calls[0][1]["enabled"] is True
    assert calls[0][1]["expected_revision"] == 1
    assert synced == [(123, None, result["state"])]


def test_niveis_de_nightcore_e_reverb_escalam_o_padrao_sem_inverter_o_efeito() -> None:
    from cogs.musica.runtime_telefone.agente.efeitos import filtros, velocidade

    assert velocidade(nightcore=True, nightcore_level=1) == 1.25
    assert velocidade(nightcore=True, nightcore_level=2) == 1.50
    assert velocidade(nightcore=True, nightcore_level=3) == 1.75
    assert velocidade(nightcore=False, slowed_reverb=True, slowed_reverb_level=1) == 0.80
    assert velocidade(nightcore=False, slowed_reverb=True, slowed_reverb_level=2) == 0.60
    assert velocidade(nightcore=False, slowed_reverb=True, slowed_reverb_level=3) == 0.40

    night2 = filtros(bassboost=False, nightcore=True, nightcore_level=2)
    night3 = filtros(bassboost=False, nightcore=True, nightcore_level=3)
    assert "asetrate=72000" in night2
    assert "asetrate=84000" in night3

    reverb2 = filtros(bassboost=False, nightcore=False, slowed_reverb=True, slowed_reverb_level=2)
    reverb3 = filtros(bassboost=False, nightcore=False, slowed_reverb=True, slowed_reverb_level=3)
    assert "asetrate=28800" in reverb2
    assert "0.54|0.40|0.28|0.18" in reverb2
    assert "asetrate=19200" in reverb3
    assert "0.81|0.60|0.42|0.27" in reverb3
    assert "volume=3.04" not in reverb2 and "volume=4.56" not in reverb3


@pytest.mark.asyncio
async def test_comando_de_efeito_aceita_niveis_e_preserva_protocolo_booleano(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    st = music.GuildMusicState(guild_id=777)
    agent.states[777] = st

    night = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "nightcore", "level": 2, "expected_revision": 0,
    })
    assert night["ok"]
    assert st.nightcore and st.nightcore_level == 2 and st.playback_speed == 1.5
    assert night["state"]["nightcore_level"] == 2

    reverb = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "slowed_reverb", "level": 3, "expected_revision": 1,
    })
    assert reverb["ok"]
    assert not st.nightcore and st.nightcore_level == 0
    assert st.slowed_reverb and st.slowed_reverb_level == 3 and st.playback_speed == 0.4

    bass = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "bassboost", "level": 6, "expected_revision": 2,
    })
    assert bass["ok"] and st.bassboost and st.bassboost_level == 6

    invalid_bass = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "bassboost", "level": 7, "expected_revision": 3,
    })
    assert not invalid_bass["ok"] and st.effects_revision == 3 and st.bassboost_level == 6

    legacy_off = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "bassboost", "enabled": False, "expected_revision": 3,
    })
    assert legacy_off["ok"] and not st.bassboost and st.bassboost_level == 0

    legacy_on = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "nightcore", "enabled": True, "expected_revision": 4,
    })
    assert legacy_on["ok"] and st.nightcore_level == 1 and st.playback_speed == 1.25

    invalid = await agent.cmd_audio_effect({
        "guild_id": 777, "effect": "nightcore", "level": 4, "expected_revision": 5,
    })
    assert not invalid["ok"] and st.effects_revision == 5 and st.nightcore_level == 1


@pytest.mark.asyncio
async def test_bassboost_nivel_muda_no_mixer_sem_reiniciar_decoder(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    agent._loop = asyncio.get_running_loop()

    class PCM(music.discord.AudioSource):
        def read(self): return b"\x01\x00" * 1920
        def cleanup(self): pass

    source = PCM()
    mixer = music.AgentMixedAudioSource(
        loop=agent._loop, music_source=source, music_volume=0.55, persistent=True,
    )

    class Voice:
        source = mixer
        def is_playing(self): return True
        def is_paused(self): return False

    st = music.GuildMusicState(
        guild_id=778, current=music.AgentTrack(stream_url="https://cdn.invalid/song"),
        player=Voice(), status="playing", transport="direct",
        started_monotonic=time.monotonic() - 4, playback_token=9,
    )
    agent.states[778] = st
    agent._create_pcm_source = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("decoder reiniciado"))
    try:
        for revision, level in enumerate((1, 2, 3, 4, 5, 6)):
            result = await agent.cmd_audio_effect({
                "guild_id": 778, "effect": "bassboost", "level": level,
                "expected_revision": revision,
            })
            assert result["ok"] and st.bassboost_level == level and mixer.bassboost_level == level
            assert mixer.music_source is source and st.playback_token == 9
    finally:
        mixer.cleanup()


@pytest.mark.asyncio
async def test_snapshot_sincroniza_niveis_2_e_3_e_velocidades_extremas() -> None:
    router = _RouterSyncFalso()
    current = {"title": "faixa", "webpage_url": "https://example.invalid/faixa", "duration": 100}
    base = {"status": "playing", "confirmed_playing": True, "voice_connected": True,
            "player_present": True, "current": current}

    await sincronizar_estado_agente(router, 42, agent_state={
        **base, "playback_token": 1, "position_ms": 10000,
        "nightcore": True, "nightcore_level": 3,
        "speed_multiplier": 1.75, "effects_revision": 1,
    }, create_panel=False)
    st = router.state
    assert st.nightcore and st.nightcore_level == 3 and st.playback_speed == 1.75

    await sincronizar_estado_agente(router, 42, agent_state={
        **base, "playback_token": 2, "position_ms": 12000,
        "nightcore": False, "nightcore_level": 0,
        "slowed_reverb": True, "slowed_reverb_level": 3,
        "speed_multiplier": 0.4, "effects_revision": 2,
    }, create_panel=False)
    assert not st.nightcore and st.nightcore_level == 0
    assert st.slowed_reverb and st.slowed_reverb_level == 3 and st.playback_speed == 0.4

    await sincronizar_estado_agente(router, 42, agent_state={
        **base, "playback_token": 3, "position_ms": 13000,
        "bassboost": True, "bassboost_level": 6, "effects_revision": 3,
    }, create_panel=False)
    assert st.bassboost and st.bassboost_level == 6


@pytest.mark.asyncio
async def test_controle_remoto_envia_nivel_e_flag_compativel(monkeypatch) -> None:
    calls = []

    async def command(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True, "state": {"status": "playing", "bassboost": True,
                                       "bassboost_level": 6, "effects_revision": 8}}

    async def sync(*_args, **_kwargs):
        return None

    monkeypatch.setattr(controle_remoto, "music_agent_command", command)
    result = await controle_remoto.ajustar_efeito(
        SimpleNamespace(sync_music_agent_state=sync), 123, "bassboost", True,
        level=6, expected_revision=7,
    )
    assert result["ok"]
    assert calls[0][0] == "audio_effect"
    assert calls[0][1]["level"] == 6 and calls[0][1]["enabled"] is True
    assert calls[0][1]["expected_revision"] == 7


def test_bassboost_niveis_escalam_intensidade_sem_clipping(monkeypatch) -> None:
    music = _load_music_agent(monkeypatch)
    rate = 48000
    tone = array("h")
    for i in range(rate):
        value = int(32767 * 0.02 * math.sin(2 * math.pi * 80 * i / rate))
        tone.extend((value, value))

    async def render(level: int) -> array:
        class Source(music.discord.AudioSource):
            def __init__(self): self.index = 0
            def read(self):
                start = self.index * 1920
                self.index += 1
                return tone[start : start + 1920].tobytes()
            def cleanup(self): pass

        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(), music_source=Source(), music_volume=0.55,
            persistent=True, bassboost_level=level,
        )
        output = array("h")
        try:
            for _ in range(50):
                output.frombytes(mixer.read())
        finally:
            mixer.cleanup()
        return output

    def amplitude(samples: array) -> float:
        start, end = rate // 2, rate
        window = samples[start * 2 : end * 2 : 2]
        sine = sum(value * math.sin(2 * math.pi * 80 * (i + start) / rate)
                   for i, value in enumerate(window))
        cosine = sum(value * math.cos(2 * math.pi * 80 * (i + start) / rate)
                     for i, value in enumerate(window))
        return 2 * math.hypot(sine, cosine) / len(window)

    async def scenario() -> None:
        levels = [await render(level) for level in range(7)]
        amplitudes = [amplitude(samples) for samples in levels]
        assert all(left < right for left, right in zip(amplitudes, amplitudes[1:]))
        # O ramo reforçado cresce de 1x até 6x; o sinal seco continua presente.
        added = [amplitudes[index] - amplitudes[0] for index in range(1, 7)]
        for multiplier, value in enumerate(added, start=1):
            ratio = value / added[0]
            assert multiplier * 0.93 <= ratio <= multiplier * 1.07
        assert max(max(map(abs, samples)) for samples in levels) <= 32000

    asyncio.run(scenario())
