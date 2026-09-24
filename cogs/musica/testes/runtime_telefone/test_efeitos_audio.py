from __future__ import annotations

import asyncio
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
    with pytest.raises(ValueError, match="personalizado"):
        agent._ffmpeg_options_for_source(48000, effects=(True, False))
    agent.ffmpeg_options = "-vn -sn -dn -loglevel warning"
    assert agent._ffmpeg_options_for_source(48000, effects=(False, True), is_live=True)[0] == agent.ffmpeg_options


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

    def create(_track, *, effects=(False, False)):
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
        assert initial.cleaned and emitted[0][0] == (False, True)

        st.paused = True
        st.status = "paused"
        st.paused_monotonic = time.monotonic()
        st.player.is_playing = lambda: False
        st.player.is_paused = lambda: True
        paused_position = st.source_position_seconds()
        second = await agent.cmd_audio_effect({"guild_id": gid, "effect": "bassboost", "enabled": True,
                                               "expected_revision": 1})
        assert second["ok"] and st.bassboost and st.nightcore
        assert emitted[1][0] == (True, True) and emitted[0][1].cleaned
        assert st.queue == [queued] and st.paused and st.status == "paused"
        assert abs(st.source_position_seconds(now=st.paused_monotonic + 10) - paused_position) < 0.1

        stale = await agent.cmd_audio_effect({"guild_id": gid, "effect": "nightcore", "enabled": False,
                                              "expected_revision": 0})
        assert not stale["ok"] and len(emitted) == 2 and st.nightcore
        assert st.source_position_seconds() >= st.current.start_offset_seconds
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
        result = await agent.cmd_audio_effect({"guild_id": 11, "effect": "bassboost", "enabled": True,
                                                "expected_revision": 0})
        assert not result["ok"] and not st.bassboost and st.playback_token == 0
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
                               queue=[track], bassboost=True)
    agent.states[51] = st
    raw = PCM()
    source = BufferedPCMSource(raw, max_frames=2)
    await source.wait_ready(timeout=1)
    agent._prepared_audio[51] = AudioPreparado(track.queue_item_id, track.stream_url, 0,
                                                source, time.monotonic(), True, (False, False))
    assert agent._take_prepared_audio(51, track) is None
    assert raw.cleaned


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
