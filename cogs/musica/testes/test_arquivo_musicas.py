from __future__ import annotations

import pytest

from cogs.musica.busca import arquivo
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.arquivo_coordenador import ArchiveCoordinator
from cogs.musica.runtime_telefone.agente.validade_stream import (
    ArchiveMixin, DiscordAttachmentError, _archive_audio_filename, _archive_metadata, _archive_url,
)


def _track(duration=240, *, webpage="https://www.youtube.com/watch?v=abc123", original=""):
    return MusicTrack(title="Música de teste", webpage_url=webpage, original_url=original,
                      duration=duration, requester_id=1, source="YouTube", queue_item_id="play-1")


def test_duas_reproducoes_reais_limite_e_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    track = _track(original="https://open.spotify.com/track/song-1")
    key = arquivo.media_key(track)
    arquivo.set_channel(77, 88)
    assert arquivo.record_play(track, "1:play-1:1")
    assert arquivo.counts()["one_play"] == 1
    assert not arquivo.record_play(track, "1:play-1:1")
    assert arquivo.pending() is None
    assert arquivo.record_play(track, "1:play-2:2")
    assert arquivo.counts()["waiting"] == 1
    assert arquivo.pending()["key"] == key
    ref = {"guild_id": 77, "channel_id": 88, "message_id": 99, "attachment_id": 100}
    arquivo.mark_result(key, {"status": "done", "reference": ref, "emoji": "<:YouTube:123>"})
    # Busca aprendida pode conter só o link original Spotify.
    from_origin = _track(webpage="https://open.spotify.com/track/song-1", original="")
    assert arquivo.archived(from_origin) == (ref, "<:YouTube:123>", key)
    from cogs.musica.agente_telefone.protocolo import faixa_para_payload
    assert faixa_para_payload(from_origin)["archive_ref"] == ref
    assert faixa_para_payload(from_origin)["archive_key"] == key
    arquivo.set_channel(0, 0)
    arquivo.set_channel(77, 88)
    assert arquivo.archived(from_origin) == (ref, "<:YouTube:123>", key)
    assert not arquivo.record_play(_track(duration=600.01), "1:too-long:3")
    assert arquivo.record_play(_track(duration=600), "1:exactly-ten:3")
    arquivo.set_channel(77, 101)
    assert arquivo.archived(from_origin) == ({}, "", "")
    assert arquivo.pending()["key"] == key


def test_observador_conta_dois_inicios_e_nao_conta_fila_snapshot_seek_ou_pausa(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    monkeypatch.setattr("cogs.musica.busca.memoria.faixa_aprendida", lambda track: True)
    track = _track(duration=120)
    manager = ArchiveCoordinator(object())
    arquivo.set_channel(1, 2)
    first = {"position_ms": 0, "playback_token": 1, "last_event": "direct_track_start_confirmed"}
    manager.observe(1, track, first, confirmed=False)  # adicionado à fila
    with arquivo._db() as db:
        assert db.execute("SELECT COUNT(*) FROM arquivo_reproducoes").fetchone()[0] == 0
    manager.observe(1, track, first, confirmed=True)  # tocou, mesmo que pause em seguida
    for position, token, event, confirmed in ((0, 1, "direct_track_start_confirmed", True),
                                               (100, 2, "seek", True), (100, 2, "pause", False)):
        manager.observe(1, track, {"position_ms": position * 1000,
                        "playback_token": token, "last_event": event}, confirmed=confirmed)
    with arquivo._db() as db:
        assert db.execute("SELECT tocadas FROM arquivo_musicas").fetchone()[0] == 1
    track.queue_item_id = "play-2"
    manager.observe(1, track, {"position_ms": 0, "playback_token": 3,
                    "last_event": "direct_track_start_confirmed"}, confirmed=True)
    assert arquivo.pending()["key"] == arquivo.media_key(track)
    assert manager._wake.is_set()


def test_falha_de_upload_visivel_e_retentavel(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    track = _track()
    arquivo.set_channel(1, 2)
    arquivo.record_play(track, "play-1")
    arquivo.record_play(track, "play-2")
    key = arquivo.media_key(track)
    arquivo.mark_result(key, {"status": "failed"})
    assert arquivo.counts()["failed"] == 1
    assert arquivo.pending() is None  # aguarda o backoff antes de tentar de novo


def test_arquivo_antigo_entra_em_atualizacao_sem_nova_reproducao(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    track = _track()
    arquivo.set_channel(1, 2)
    arquivo.record_play(track, "primeiro")
    arquivo.record_play(track, "segundo")
    key = arquivo.media_key(track)
    old = {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4}
    arquivo.mark_result(key, {"status": "done", "reference": old, "emoji": "🎵"})
    assert arquivo.pending()["reference"] == old
    assert arquivo.counts()["refresh"] == 1
    new = {**old, "attachment_id": 9}
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 2})
    assert arquivo.pending() is None
    assert arquivo.archived(track)[0] == new


def test_migracao_banco_antigo_preserva_referencia(tmp_path, monkeypatch):
    import json
    import sqlite3
    from cogs.musica.busca.memoria import _track_payload

    base = tmp_path / "escolhas.sqlite3"
    monkeypatch.setattr(arquivo, "_db_path", lambda: base)
    path = tmp_path / "escolhas-arquivo.sqlite3"
    track = _track()
    ref = {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE arquivo_config (id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER)")
        db.execute("INSERT INTO arquivo_config VALUES (1, 1, 2)")
        db.execute("""CREATE TABLE arquivo_musicas (
            chave TEXT PRIMARY KEY, track_json TEXT, tocadas INTEGER, reference_json TEXT,
            emoji TEXT, estado TEXT, tentativa_em REAL, falhas INTEGER)""")
        db.execute("INSERT INTO arquivo_musicas VALUES (?, ?, 2, ?, '🎵', 'done', 0, 0)",
                   (arquivo.media_key(track), json.dumps(_track_payload(track)), json.dumps(ref)))
    assert arquivo.pending()["reference"] == ref
    assert arquivo.archived(track)[0] == ref
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT apresentacao FROM arquivo_musicas").fetchone()[0] == 1


def test_mensagem_do_arquivo_fornece_emoji_e_exige_autoria():
    key = "a" * 32
    ref = {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4}
    raw = {"author": {"id": "5"}, "guild_id": "1", "embeds": [{"footer": {"text": "music-archive:v1:" + key},
           "fields": [{"name": "Fonte", "value": "<:YouTube:123> YouTube"},
                      {"name": "Duração", "value": "234.50"}, {"name": "Índice de áudio", "value": "0"}]}],
           "attachments": [{"id": "4", "filename": "musica-a.webm",
                            "url": "https://cdn.discordapp.com/attachments/2/4/musica-a.webm?ex=fffffff&hm=abc"}]}
    result = _archive_metadata(raw, key, 5, ref)
    assert result["emoji"] == "<:YouTube:123>"
    assert result["audio_stream_index"] == 0
    raw["embeds"][0] = {"url": _archive_url("https://www.youtube.com/watch?v=abc", key),
                          "footer": {"text": "Arquivo de músicas"},
                          "fields": [{"name": "Fonte", "value": "<:YouTube:123> YouTube"},
                                     {"name": "Duração", "value": "3:55"},
                                     {"name": "Formato", "value": "OPUS · OGG"}]}
    raw["attachments"][0]["filename"] = "Arctic Monkeys - 505.ogg"
    assert _archive_metadata(raw, key, 5, ref)["duration"] == 235.0
    assert _archive_metadata(raw, key, 5, ref)["audio_stream_index"] == 0
    assert _archive_audio_filename("Arctic Monkeys / 505", ".ogg") == "Arctic Monkeys 505.ogg"
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", "b" * 32)
    with pytest.raises(DiscordAttachmentError):
        _archive_metadata(raw, key, 5, ref)
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key)
    raw["author"]["id"] = "6"
    try:
        _archive_metadata(raw, key, 5, ref)
    except DiscordAttachmentError:
        pass
    else:
        raise AssertionError("Mensagem de outro autor foi aceita")


def test_agendamento_no_agente_responde_antes_do_download():
    import asyncio

    class Stub(ArchiveMixin):
        def __init__(self):
            self._archive_init()
            self.calls = []

        async def _archive_one(self, item):
            self.calls.append(item["key"])
            await asyncio.sleep(0.02)
            return {"status": "done", "reference": {"guild_id": 1, "channel_id": 2,
                                                      "message_id": 3, "attachment_id": 4}, "emoji": "🎵"}

        def log(self, *args, **kwargs):
            pass

    async def check():
        stub = Stub()
        key = "a" * 32
        body = {"archive_key": key, "guild_id": 1, "archive_channel_id": 2,
                "track": {"webpage_url": "https://www.youtube.com/watch?v=abc", "duration": 240}}
        assert (await stub.cmd_archive_enqueue(body))["status"] == "queued"
        assert (await stub.cmd_archive_enqueue(body))["status"] == "queued"
        await stub._archive_queue.join()
        assert stub.calls == [key]
        assert (await stub.cmd_archive_status(body))["status"] == "done"

    asyncio.run(check())


def test_estado_compacto_inclui_progresso_sem_serializar_fila():
    import time
    from types import SimpleNamespace
    from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
    from cogs.musica.runtime_telefone.agente.servidor import MusicAgent

    agent = MusicAgent()
    state = GuildMusicState(
        guild_id=91, current=AgentTrack(title="Faixa", query="q", queue_item_id="play-1"),
        status="playing", started_monotonic=time.monotonic() - 12, playback_token=4,
    )
    state.player = SimpleNamespace(is_connected=lambda: True, is_playing=lambda: True)
    agent.states[91] = state
    revision = state.state_revision()
    payload = agent.status_payload(guild_id=91, compact=True, known_revision=revision)

    assert payload["unchanged"] is True
    assert payload["guilds"] == {}
    assert payload["archive_progress"]["queue_item_id"] == "play-1"
    assert payload["archive_progress"]["confirmed_playing"] is True
    assert payload["archive_progress"]["position_ms"] >= 11000
    assert payload["archive_progress"]["playback_token"] == 4


@pytest.mark.asyncio
async def test_monitor_conta_progresso_compacto_da_faixa_certa(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from cogs.musica.agente_telefone import monitor

    original_sleep = asyncio.sleep
    state = SimpleNamespace(agent_monitor_task=None, now_message=object(), current_status="playing",
                            current=SimpleNamespace(queue_item_id="play-1"))
    observed = []

    class Router:
        archive = SimpleNamespace(observe=lambda *args, **kwargs: observed.append((args, kwargs)))

        def get_state(self, guild_id):
            return state

        async def sync_music_agent_state(self, guild_id, track, remote, **kwargs):
            pass

    calls = 0

    async def status_fake(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": True, "available": True, "guilds": {"91": {
                "status": "playing", "confirmed_playing": True, "voice_connected": True,
                "player_present": True, "state_revision": "rev-1", "playback_token": 4,
                "current": {"queue_item_id": "play-1"}, "queue": [], "queue_size": 0,
            }}}
        if calls in (2, 3):
            return {"ok": True, "available": True, "unchanged": True, "state_revision": "rev-1",
                    "guilds": {}, "archive_progress": {"queue_item_id": "play-1" if calls == 2 else "other",
                    "position_ms": calls * 10000, "playback_token": 4, "confirmed_playing": True}}
        return {"ok": False, "available": False, "error": "offline"}

    async def yield_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_sleep)
    monitor.iniciar_monitor_music_agent(Router(), 91)
    await asyncio.wait_for(state.agent_monitor_task, timeout=1.0)

    assert len(observed) == 1
    assert observed[0][0][0] == 91
    assert observed[0][0][1] is state.current
    assert observed[0][0][2]["position_ms"] == 20000
    assert observed[0][1] == {"confirmed": True}


@pytest.mark.asyncio
async def test_webm_opus_vira_ogg_sem_recodificar_e_capa_vira_jpeg(tmp_path):
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg indisponível")
    webm = tmp_path / "audio.webm"
    image = tmp_path / "entrada.png"
    try:
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "libopus", "-y", str(webm)],
                       check=True, capture_output=True)
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=64x64:d=1", "-frames:v", "1", "-y", str(image)],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("Encoders de teste indisponíveis")
    class Worker(ArchiveMixin):
        ffmpeg_executable = ffmpeg
        ffprobe_executable = ffprobe

    worker = Worker()
    ready = await worker._archive_audio_ready(webm, tmp_path)
    assert ready is not None
    audio, duration, index, codec = ready
    assert audio.suffix == ".ogg"
    assert codec == "opus" and index == 0 and 0.9 < duration < 1.1
    jpeg = await worker._archive_jpeg(image, tmp_path / "capa.jpg")
    assert jpeg is not None and jpeg.read_bytes().startswith(b"\xff\xd8\xff")


@pytest.mark.asyncio
async def test_atualizacao_da_mensagem_antiga_mantem_id_e_muda_anexo(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    # Um teste legado recarrega o módulo Discord durante a coleta. Use o
    # módulo efetivamente associado ao ArchiveMixin sob teste.
    discord = ArchiveMixin._archive_one.__globals__["discord"]

    key = "a" * 32
    original = "https://www.youtube.com/watch?v=abc"
    old_embed = discord.Embed(title="Arctic Monkeys - 505", url=original)
    old_embed.add_field(name="Fonte", value="🎵 YouTube")
    old_embed.add_field(name="Duração", value="254.08")
    old_embed.add_field(name="Índice de áudio", value="0")
    old_embed.set_footer(text="music-archive:v1:" + key)

    class Attachment:
        id = 4
        filename = "musica-" + key + ".webm"
        size = 50

        async def save(self, destination):
            Path(destination).write_bytes(b"webm-opus")

    class Message:
        id = 3
        author = SimpleNamespace(id=5)
        embeds = [old_embed]
        attachments = [Attachment()]

        async def edit(self, *, embed, attachments, allowed_mentions):
            self.embeds = [embed]
            self.attachments = [SimpleNamespace(id=9, filename=attachments[0].filename)]
            return self

    message = Message()

    class Channel:
        id = 2
        guild = SimpleNamespace(id=1)

        async def fetch_message(self, message_id):
            assert message_id == 3
            return message

    channel = Channel()
    monkeypatch.setattr(discord, "TextChannel", Channel, raising=False)

    async def ready():
        pass

    async def raw(channel_id, message_id):
        attachment = message.attachments[0]
        return {"author": {"id": "5"}, "guild_id": "1", "embeds": [message.embeds[0].to_dict()],
                "attachments": [{"id": str(attachment.id), "filename": attachment.filename,
                                 "url": f"https://cdn.discordapp.com/attachments/2/{attachment.id}/{attachment.filename}"}]}

    class Worker(ArchiveMixin):
        def __init__(self):
            self.client = SimpleNamespace(wait_until_ready=ready, get_channel=lambda _: channel,
                                          user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=raw))
            self.states = {}

        async def _archive_wait_stable_voice(self):
            pass

        async def _archive_audio_ready(self, audio, folder):
            output = folder / "audio.ogg"
            output.write_bytes(audio.read_bytes())
            return output, 254.08, 0, "opus"

        async def _archive_cover(self, url, folder):
            output = folder / "capa.jpg"
            output.write_bytes(b"\xff\xd8\xff")
            return output

    item = {"key": key, "guild_id": 1, "channel_id": 2, "emoji": "🎵",
            "existing_ref": {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4},
            "track": {"title": "Arctic Monkeys - 505", "source": "YouTube",
                      "webpage_url": original, "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg"}}
    result = await Worker()._archive_one(item)
    assert result["status"] == "done" and result["presentation"] == 2
    assert result["reference"]["message_id"] == 3
    assert result["reference"]["attachment_id"] == 9
    assert message.attachments[0].filename == "Arctic Monkeys - 505.ogg"
    assert message.embeds[0].footer.text == "Arquivo de músicas"
    assert message.embeds[0].thumbnail.url == "attachment://capa.jpg"
    assert [field.name for field in message.embeds[0].fields] == ["Fonte", "Duração", "Formato"]
