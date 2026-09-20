from __future__ import annotations

from pathlib import Path

from cogs.musica.runtime_telefone.agente.utilitarios import (
    DEFAULT_YTDLP_AUDIO_FORMAT,
    audio_format_score,
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


def test_default_ytdlp_format_prioriza_opus_48k_com_fallbacks() -> None:
    assert DEFAULT_YTDLP_AUDIO_FORMAT.startswith("bestaudio[acodec=opus][asr=48000]/")
    assert "/bestaudio[acodec=opus]/" in DEFAULT_YTDLP_AUDIO_FORMAT
    assert "/bestaudio[asr=48000]/" in DEFAULT_YTDLP_AUDIO_FORMAT
    assert DEFAULT_YTDLP_AUDIO_FORMAT.endswith("bestaudio/best")


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
