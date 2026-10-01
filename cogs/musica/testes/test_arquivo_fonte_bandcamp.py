"""Fonte oficial alternativa para uma faixa aprendida sem mídia no YouTube."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType

import pytest

from cogs.musica.busca import arquivo
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.arquivo_fonte import resolve_bandcamp_source
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, _archive_bandcamp_source, _archive_download_reason


PAGE = "https://heavenpierceher.bandcamp.com/track/disgrace-humiliation"
STREAM = "https://t4.bcbits.com/stream/album/track/mp3-128?token=temporario"


def _track() -> MusicTrack:
    return MusicTrack(
        title="Heaven Pierce Her - Disgrace. Humiliation.",
        display_title="Heaven Pierce Her - Disgrace. Humiliation.",
        display_uploader="Heaven Pierce Her", duration=110,
        webpage_url="https://open.spotify.com/track/58PgKGG6epmtd9Nyp8cFHf",
        original_url="https://open.spotify.com/track/58PgKGG6epmtd9Nyp8cFHf",
        requester_id=1, source="Spotify",
    )


def test_fonte_oficial_reagenda_sem_mudar_chave_aprendida(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "escolhas.sqlite3")
    arquivo.set_channel(1, 2, kind="forum")
    track = _track()
    key = arquivo.media_key(track)
    assert arquivo.record_play(track, "inicio")
    arquivo.mark_result(key, {"status": "failed"})
    assert arquivo.pending() is None
    arquivo.set_source_override(key, PAGE)
    pending = arquivo.pending()
    assert pending["key"] == key and pending["source_override"] == PAGE
    assert pending["track"]["original_url"] == track.original_url
    with arquivo._db() as db:
        assert db.execute("SELECT falhas, tentativa_em FROM arquivo_musicas WHERE chave=?", (key,)).fetchone() == (0, 0)
    with pytest.raises(ValueError):
        arquivo.set_source_override(key, "https://evil.example/track/other")
    with pytest.raises(ValueError):
        arquivo.set_source_override(key, "https://heavenpierceher.bandcamp.com:bad/track/disgrace-humiliation")
    with pytest.raises(ValueError):
        arquivo.set_source_override("a" * 32, PAGE)


def test_vps_aceita_apenas_audio_oficial_com_titulo_e_duracao_compativeis(monkeypatch):
    info = {"title": "Heaven Pierce Her - Disgrace. Humiliation.", "duration": 110.014,
            "url": STREAM, "acodec": "mp3", "abr": 128}

    class YDL:
        def __init__(self, options):
            assert options["skip_download"] and options["format"] == "bestaudio/best"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, url, download=False):
            assert url == PAGE and download is False
            return dict(info)

    yt_dlp = ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = YDL
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    monkeypatch.setitem(sys.modules, "curl_cffi", ModuleType("curl_cffi"))
    expected = {"display_title": _track().display_title, "display_uploader": "Heaven Pierce Her", "duration": 110}
    resolved = resolve_bandcamp_source(PAGE, expected)
    assert resolved["stream_url"] == STREAM and resolved["source"] == "Bandcamp"

    info["duration"] = 1800.0
    expected["duration"] = 1800.0
    assert resolve_bandcamp_source(PAGE, expected)["duration"] == 1800.0
    info["duration"] = 110.014
    expected["duration"] = 110
    for field, wrong in (("title", "Heaven Pierce Her - ORDER"), ("title", ""), ("duration", 220),
                         ("url", "https://127.0.0.1/audio.mp3")):
        modified = info[field]
        info[field] = wrong
        with pytest.raises(RuntimeError):
            resolve_bandcamp_source(PAGE, expected)
        info[field] = modified


@pytest.mark.asyncio
async def test_agent_baixa_fonte_validada_sem_refazer_busca_spotify(tmp_path, monkeypatch):
    from cogs.musica.runtime_telefone.agente import validade_stream as module

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    commands = []

    async def spawn(*cmd, **_kwargs):
        commands.append(cmd)
        (tmp_path / "audio.mp3").write_bytes(b"audio")
        return Process()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)

    class Worker(ArchiveMixin):
        states = {}
        cookies_file = ""
        js_runtimes = ""
        _archive_active = "a" * 32

        async def resolve_track(self, *_args, **_kwargs):
            raise AssertionError("não deve consultar o Spotify/YouTube")

    raw = {"webpage_url": PAGE, "stream_url": STREAM, "duration": 110.014,
           "title": "Heaven Pierce Her - Disgrace. Humiliation."}
    track = {"webpage_url": _track().webpage_url, "original_url": _track().original_url,
             "display_title": _track().display_title, "display_uploader": "Heaven Pierce Her", "duration": 110}
    source = _archive_bandcamp_source(raw, track)
    item = {"key": "a" * 32, "guild_id": 1, "track": track, "archive_source": source,
            "emoji": "🎵", "source_emojis": {"bandcamp": "🎼"}}
    audio = await Worker()._archive_download(item, tmp_path)
    assert audio == tmp_path / "audio.mp3"
    assert commands[0][-1] == STREAM and commands[0][commands[0].index("-f") + 1] == "bestaudio/best"
    assert track["webpage_url"] == track["original_url"] == PAGE
    assert track["display_source"] == "Bandcamp" and item["emoji"] == "🎼"
    with pytest.raises(ValueError):
        _archive_bandcamp_source({**raw, "stream_url": "https://localhost/audio"}, track)
    assert _archive_download_reason(f"ERROR: HTTP Error 403: Forbidden ({STREAM})".encode(), bandcamp=True) == "source_http_403"


@pytest.mark.asyncio
async def test_coordenador_resolve_fonte_e_a_entrega_ao_agent(tmp_path, monkeypatch):
    from cogs.musica import arquivo_coordenador as coordinator
    from cogs.musica import arquivo_fonte

    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "escolhas.sqlite3")
    arquivo.set_channel(1, 2, kind="forum")
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "inicio")
    arquivo.set_source_override(key, PAGE)
    assert not coordinator._source_agent_ready({"available": True, "version": "0.3.79"})
    assert not coordinator._source_agent_ready({"available": True, "version": "0.3.80"})
    assert not coordinator._source_agent_ready({"available": True, "version": "0.3.81"})
    assert coordinator._source_agent_ready({"available": True, "version": "0.3.82"})

    class Bot:
        async def wait_until_ready(self):
            pass

    sent = []
    async def status(**_kwargs):
        return {"available": True, "version": "0.3.82"}

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        if action == "archive_enqueue":
            return {"ok": True, "status": "queued"}
        return {"status": "done", "presentation": 7,
                "reference": {"guild_id": 1, "forum_id": 2, "channel_id": 3,
                              "message_id": 4, "attachment_id": 5}}

    def resolve(url, metadata):
        assert url == PAGE and metadata["original_url"] == track.original_url
        return {"webpage_url": PAGE, "stream_url": STREAM, "duration": 110.014,
                "source": "Bandcamp", "title": track.title}

    real_sleep = asyncio.sleep
    real_pending = arquivo.pending
    pending_calls = 0

    def pending(**kwargs):
        nonlocal pending_calls
        pending_calls += 1
        if pending_calls > 1:
            raise asyncio.CancelledError
        return real_pending(**kwargs)

    async def sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(coordinator, "music_agent_status", status)
    monkeypatch.setattr(coordinator, "music_agent_command", command)
    monkeypatch.setattr(arquivo_fonte, "resolve_bandcamp_source", resolve)
    monkeypatch.setattr(arquivo, "pending", pending)
    monkeypatch.setattr(coordinator.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.ArchiveCoordinator(Bot())._run()
    assert sent[0][0] == "archive_enqueue" and sent[0][1]["archive_source"]["stream_url"] == STREAM
    assert sent[0][1]["source_emoji"] == "🎼"
    assert arquivo.archived(track)[0]["forum_id"] == 2
