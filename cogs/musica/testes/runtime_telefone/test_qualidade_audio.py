from __future__ import annotations

from pathlib import Path

from cogs.musica.runtime_telefone.agente.utilitarios import (
    DEFAULT_YTDLP_AUDIO_FORMAT,
    DEFAULT_YTDLP_AUDIO_SORT,
    audio_format_score,
    formato_audio_configurado,
    LEGACY_YTDLP_AUDIO_FORMAT,
    select_stream_info,
)

ROOT = Path(__file__).resolve().parents[4]


def _fmt(
    url: str,
    *,
    acodec: str,
    abr: int,
    asr: int = 48000,
    channels: int = 2,
    vcodec: str = "none",
    format_id: str = "251",
    ext: str = "webm",
) -> dict:
    return {
        "url": url,
        "acodec": acodec,
        "vcodec": vcodec,
        "abr": abr,
        "asr": asr,
        "audio_channels": channels,
        "format_id": format_id,
        "ext": ext,
    }


def test_default_ytdlp_format_equilibra_bitrate_codec_e_rate() -> None:
    assert DEFAULT_YTDLP_AUDIO_FORMAT == "bestaudio/best"
    assert DEFAULT_YTDLP_AUDIO_SORT == "abr,acodec,asr"
    assert formato_audio_configurado(LEGACY_YTDLP_AUDIO_FORMAT) == DEFAULT_YTDLP_AUDIO_FORMAT
    assert formato_audio_configurado("bestaudio[ext=m4a]/best") == "bestaudio[ext=m4a]/best"


def test_select_stream_preserva_escolha_final_do_ytdlp_e_telemetria() -> None:
    selected = _fmt("https://cdn/selected", acodec="opus", abr=160, asr=48000, channels=2)
    entry = {
        "requested_downloads": [selected],
        "formats": [_fmt("https://cdn/other", acodec="mp4a.40.2", abr=256, asr=44100, channels=2)],
    }
    info = select_stream_info(entry)
    assert info == {
        "stream_url": "https://cdn/selected",
        "audio_format_id": "251",
        "audio_ext": "webm",
        "audio_codec": "opus",
        "audio_abr": 160,
        "audio_sample_rate": 48000,
        "audio_channels": 2,
    }


def test_scoring_prefere_opus_48k_quando_qualidade_eh_proxima() -> None:
    opus = _fmt("https://cdn/opus", acodec="opus", abr=160, asr=48000, channels=2)
    aac = _fmt("https://cdn/aac", acodec="mp4a.40.2", abr=128, asr=44100, channels=2, format_id="140", ext="m4a")
    assert audio_format_score(opus) > audio_format_score(aac)
    assert select_stream_info({"formats": [aac, opus]})["stream_url"] == "https://cdn/opus"


def test_scoring_nao_escolhe_opus_muito_comprimido_so_pelo_codec() -> None:
    opus_low = _fmt("https://cdn/opus-low", acodec="opus", abr=64, asr=48000, channels=2)
    aac_high = _fmt("https://cdn/aac-high", acodec="mp4a.40.2", abr=256, asr=44100, channels=2, format_id="140", ext="m4a")
    assert audio_format_score(aac_high) > audio_format_score(opus_low)


def test_audio_only_ganha_de_formato_com_video() -> None:
    audio_only = _fmt("https://cdn/audio", acodec="opus", abr=128, vcodec="none")
    muxed = _fmt("https://cdn/muxed", acodec="opus", abr=192, vcodec="avc1.640028")
    assert audio_format_score(audio_only) > audio_format_score(muxed)
    assert select_stream_info({"formats": [muxed, audio_only]})["stream_url"] == "https://cdn/audio"


def test_wave_a_nao_adiciona_filtros_caros_ao_hot_path() -> None:
    server = (ROOT / "cogs/musica/runtime_telefone/agente/servidor.py").read_text(encoding="utf-8")
    playback = (ROOT / "cogs/musica/runtime_telefone/agente/reproducao.py").read_text(encoding="utf-8")
    defaults = server + playback
    for forbidden in ("loudnorm", "acompressor", "equalizer=", "firequalizer", "soxr"):
        assert forbidden not in defaults.lower()
    assert '"-vn -sn -dn -loglevel warning"' in server


def test_fast_path_coleta_codec_rate_channels_sem_nova_consulta() -> None:
    resolver = (ROOT / "cogs/musica/runtime_telefone/agente/resolucao.py").read_text(encoding="utf-8")
    for marker in (
        "__format_id__:%(format_id)s",
        "__acodec__:%(acodec)s",
        "__abr__:%(abr)s",
        "__asr__:%(asr)s",
        "__audio_channels__:%(audio_channels)s",
    ):
        assert marker in resolver
    # Continua sendo o mesmo fast path -g; apenas os campos impressos aumentaram.
    assert '"-g", target' in resolver


def test_wave_b_bitrate_opus_respeita_fonte_teto_e_canal(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()

    def voice(channel_bitrate: int):
        import types
        return types.SimpleNamespace(channel=types.SimpleNamespace(bitrate=channel_bitrate))

    track = music.AgentTrack(title="opus", query="opus", audio_abr=160)
    assert agent._discord_opus_bitrate_kbps(voice(384_000), track) == (192, 384)
    assert agent._discord_opus_bitrate_kbps(voice(128_000), track) == (128, 128)

    high = music.AgentTrack(title="high", query="high", audio_abr=320)
    assert agent._discord_opus_bitrate_kbps(voice(384_000), high) == (256, 384)

    # O limite real do canal vence inclusive o piso configurado.
    assert agent._discord_opus_bitrate_kbps(voice(64_000), track) == (64, 64)


def test_wave_b_pcm_sinaliza_music_e_bitrate_no_encoder_discord(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    calls = []

    class Source:
        def is_opus(self):
            return False

    class Voice:
        def play(self, source, **kwargs):
            calls.append((source, kwargs))

    after = lambda error: None
    source = Source()
    agent._play_music_source(Voice(), source, after=after, opus_bitrate_kbps=192)
    assert calls == [
        (
            source,
            {
                "after": after,
                "application": "audio",
                "bitrate": 192,
                "bandwidth": "full",
                "signal_type": "music",
            },
        )
    ]


def test_wave_b_source_opus_nao_cria_segundo_encoder(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    calls = []

    class Source:
        def is_opus(self):
            return True

    class Voice:
        def play(self, source, **kwargs):
            calls.append((source, kwargs))

    after = lambda error: None
    source = Source()
    agent._play_music_source(Voice(), source, after=after, opus_bitrate_kbps=224)
    assert calls == [(source, {"after": after})]


def test_wave_b_ffmpeg_opus_usa_bitrate_adaptativo(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    agent.direct_pcm_volume_enabled = False
    captured = {}

    class OpusSource:
        def __init__(self, stream_url, **kwargs):
            captured["url"] = stream_url
            captured.update(kwargs)
        def is_opus(self):
            return True

    monkeypatch.setattr(music.discord, "FFmpegOpusAudio", OpusSource, raising=False)
    source = agent._build_ffmpeg_source("https://media.example/audio", opus_bitrate_kbps=224)
    assert source.is_opus() is True
    assert captured["bitrate"] == 224


def test_wave_d_48k_permanece_no_fast_path_sem_filtro(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    base = agent.ffmpeg_options
    options, mode = agent._ffmpeg_options_for_source(48000)
    assert options == base
    assert "aresample" not in options
    assert mode == "native_48k"


def test_wave_d_resample_seletivo_so_para_fonte_nao_48k(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    options, mode = agent._ffmpeg_options_for_source(44100)
    assert mode == "swr_quality"
    assert "-af aresample=48000:resampler=swr" in options
    assert ":filter_size=64" in options
    assert ":phase_shift=10" in options
    assert ":linear_interp=1" in options
    assert ":filter_type=kaiser" in options

    agent.resample_filter_size = 32
    reduced, _ = agent._ffmpeg_options_for_source(44100)
    assert ":filter_size=32" in reduced
    assert ":exact_rational=1" in options

    unknown_options, unknown_mode = agent._ffmpeg_options_for_source(0)
    assert unknown_options == agent.ffmpeg_options
    assert unknown_mode == "ffmpeg_auto_unknown"


def test_wave_d_nao_sobrescreve_filtro_ffmpeg_customizado(monkeypatch) -> None:
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    agent.ffmpeg_options = "-vn -sn -dn -af volume=0.8 -loglevel warning"
    options, mode = agent._ffmpeg_options_for_source(44100)
    assert options == agent.ffmpeg_options
    assert mode == "custom_filter"
    assert "aresample" not in options


def test_wave_d_source_rate_chega_ao_builder_e_telemetria() -> None:
    playback = (ROOT / "cogs/musica/runtime_telefone/agente/reproducao.py").read_text(encoding="utf-8")
    assert "source_sample_rate=source_rate" in playback
    assert "resample_mode=resample_mode" in playback


def test_wave_e_telemetria_detecta_stall_sem_analisar_pcm(monkeypatch) -> None:
    import sys
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    _load_music_agent(monkeypatch)
    mixer = sys.modules["cogs.musica.runtime_telefone.agente.mixer_pcm"]
    frame = b"\x01\x00" * 1920

    class Source:
        def __init__(self):
            self.frames = [frame, frame, b""]
        def read(self):
            return self.frames.pop(0)
        def cleanup(self):
            return None
        def is_opus(self):
            return False

    # 100 ms (stall), 10 ms (normal), 1 ms (EOF).
    ticks = iter([0, 100_000_000, 100_000_000, 110_000_000, 110_000_000, 111_000_000])
    monkeypatch.setattr(mixer.time, "perf_counter_ns", lambda: next(ticks))
    source = mixer.AgentTelemetryAudioSource(
        Source(), telemetry_enabled=True, stall_threshold_ms=80, expected_frame_bytes=3840
    )
    assert source.read() == frame
    assert source.read() == frame
    assert source.read() == b""
    telemetry = source.audio_telemetry()
    assert telemetry["source_read_count"] == 3
    assert telemetry["audio_frame_count"] == 2
    assert telemetry["audio_bytes"] == 7680
    assert telemetry["source_stall_count"] == 1
    assert telemetry["source_read_max_ms"] == 100.0
    assert telemetry["partial_frame_count"] == 0


def test_wave_e_telemetria_desligada_nao_consulta_relogio_por_frame(monkeypatch) -> None:
    import sys
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    _load_music_agent(monkeypatch)
    mixer = sys.modules["cogs.musica.runtime_telefone.agente.mixer_pcm"]

    class Source:
        def read(self): return b"abc"
        def cleanup(self): return None
        def is_opus(self): return True

    source = mixer.AgentTelemetryAudioSource(Source(), telemetry_enabled=False)
    monkeypatch.setattr(
        mixer.time,
        "perf_counter_ns",
        lambda: (_ for _ in ()).throw(AssertionError("telemetria desligada tocou no relógio")),
    )
    assert source.is_opus() is True
    assert source.read() == b"abc"
    assert source.audio_telemetry()["source_read_count"] == 0


def test_wave_e_resumo_de_playback_carrega_metricas_de_qualidade(monkeypatch) -> None:
    import asyncio
    import time
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)

    async def scenario():
        agent = music.MusicAgent()
        gid = 991
        st = music.GuildMusicState(guild_id=gid)
        st.current = music.AgentTrack(title="quality", query="quality", duration=10.0)
        st.playback_token = 7
        st.started_monotonic = time.monotonic() - 10.0
        agent.states[gid] = st
        events = []
        agent.log = lambda event, **fields: events.append((event, fields))
        agent._schedule_idle_disconnect = lambda *args, **kwargs: None
        await agent._direct_after(
            gid,
            None,
            7,
            audio_metrics={"source_stall_count": 2, "source_read_max_ms": 140.0},
            quality_context={"source_codec": "opus", "resample_mode": "native_48k"},
        )
        summary = next(fields for event, fields in events if event == "audio_playback_summary")
        assert summary["outcome"] == "ended"
        assert summary["source_codec"] == "opus"
        assert summary["resample_mode"] == "native_48k"
        assert summary["source_stall_count"] == 2
        assert summary["source_read_max_ms"] == 140.0
        assert st.last_audio_end_monotonic > 0.0

    asyncio.run(scenario())


def test_wave_e_pipeline_registra_primeiro_frame_gap_e_stalls() -> None:
    playback = (ROOT / "cogs/musica/runtime_telefone/agente/reproducao.py").read_text(encoding="utf-8")
    mixer = (ROOT / "cogs/musica/runtime_telefone/agente/mixer_pcm.py").read_text(encoding="utf-8")
    for marker in (
        '"audio_pipeline_ready"',
        '"audio_playback_summary"',
        '"transition_gap_ms"',
        '"source_stall_count"',
        '"source_read_max_ms"',
    ):
        assert marker in playback + mixer
    # Telemetria não introduz análise espectral, normalização ou filtros.
    for forbidden in ("loudnorm", "astats", "ebur128", "acompressor"):
        assert forbidden not in (playback + mixer).lower()


def test_candidatos_de_catalogo_rejeitam_remix_e_duracao_errada() -> None:
    from cogs.musica.runtime_telefone.agente.correspondencia import avaliar_correspondencia, busca_alternativa

    original = {"title": "Northern Lights", "artist": "Olive", "duration": 200}
    assert avaliar_correspondencia(original, {"title": "Olive - Northern Lights", "duration": 201})[0]
    assert not avaliar_correspondencia(original, {"title": "Olive - Northern Lights live", "duration": 201})[0]
    assert not avaliar_correspondencia(original, {"title": "Olive - Northern Lights", "duration": 560})[0]
    assert busca_alternativa("ytsearch1:Olive Northern Lights") == "ytsearch3:Olive Northern Lights"


def test_busca_de_catalogo_troca_primeiro_video_errado_por_candidato_valido(monkeypatch) -> None:
    import asyncio
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    queries = []

    def resolve(query, *, expected_metadata=None):
        queries.append((query, expected_metadata))
        return {
            "title": "Other Song" if len(queries) == 1 else "Olive - Northern Lights",
            "duration": 200,
            "webpage_url": "https://youtu.be/correct",
            "stream_url": "https://cdn/correct",
        }

    agent._resolve_with_ytdlp = resolve
    meta = {"title": "Northern Lights", "artist": "Olive", "duration": 200, "source": "spotify"}
    track = asyncio.run(agent.resolve_track("ytsearch1:Olive - Northern Lights official audio", track_meta=meta, body={"guild_id": 4}))
    assert track.stream_url == "https://cdn/correct"
    assert len(queries) == 2
    assert queries[1][0].startswith("ytsearch3:")
    assert queries[1][1] == meta


def test_ytdlp_fallback_compartilha_o_prazo_e_ordena_por_qualidade(monkeypatch) -> None:
    import json
    import subprocess
    import types
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    from cogs.musica.runtime_telefone.agente import resolucao as runtime
    agent = music.MusicAgent()
    agent.cookies_file = "/nonexistent"
    agent.ytdlp_timeout = 20.0
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "false")
    clock = [100.0]
    monkeypatch.setattr(runtime, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    calls = []

    def command(cmd, *, timeout):
        calls.append((cmd, timeout))
        if "-g" in cmd:
            clock[0] += 8.0
            return subprocess.CompletedProcess(cmd, 1, "", "unavailable")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({
            "title": "song", "url": "https://cdn/audio", "acodec": "opus", "vcodec": "none",
            "abr": 160, "asr": 48000,
        }), "")

    agent._run_ytdlp_command = command
    resolved = agent._resolve_with_ytdlp("https://youtu.be/example")
    assert resolved["stream_url"] == "https://cdn/audio"
    assert len(calls) == 2
    assert calls[0][1] == 18.0
    assert calls[1][1] == 12.0
    assert all(cmd[cmd.index("--format-sort") + 1] == "abr,acodec,asr" for cmd, _timeout in calls)


def test_ytdlp_busca_alternativa_seleciona_candidato_compativel(monkeypatch) -> None:
    import json
    import subprocess
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    agent.cookies_file = "/nonexistent"

    def command(cmd, *, timeout):
        data = {"entries": [
            {"title": "Other Song", "duration": 200, "url": "https://cdn/wrong", "acodec": "opus"},
            {"title": "Olive - Northern Lights", "duration": 201, "url": "https://cdn/correct", "acodec": "opus"},
        ]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

    agent._run_ytdlp_command = command
    resolved = agent._resolve_with_ytdlp(
        "ytsearch3:Olive Northern Lights", expected_metadata={"title": "Northern Lights", "artist": "Olive", "duration": 200},
    )
    assert resolved["stream_url"] == "https://cdn/correct"


def test_tts_bufferizado_termina_apos_o_ultimo_frame(monkeypatch):
    from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent
    _load_music_agent(monkeypatch)
    from cogs.musica.runtime_telefone.agente.tts import _TimedTTSSource

    class FakeSource:
        def __init__(self):
            self.frames = [b"frame", b""]
        def read(self):
            return self.frames.pop(0)
        def cleanup(self):
            pass
        def is_opus(self):
            return False

    import time
    timed = _TimedTTSSource(FakeSource(), started=time.monotonic(), buffered=True)
    assert timed.ready.wait(timeout=1)
    assert timed.read() == b"frame"
    assert timed.read() == b""
    assert timed.read() == b""
    timed.cleanup()
