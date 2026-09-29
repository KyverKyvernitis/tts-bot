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
async def test_migracao_publica_post_com_capa_audio_e_recupera_ack_perdido(monkeypatch):
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

    message = Message()

    class Channel:
        id = 2
        guild = SimpleNamespace(id=1)

        async def fetch_message(self, message_id):
            assert message_id == 3
            return message

    channel = Channel()
    created = []

    class Thread:
        id = 40
        name = "Arctic Monkeys - 505"
        owner_id = 5

        async def history(self, **kwargs):
            yield new_message

    thread = Thread()

    class Forum:
        id = 20
        guild = SimpleNamespace(id=1)
        flags = SimpleNamespace(require_tag=False)
        available_tags = []
        threads = [thread]

        def is_media(self):
            return False

        async def archived_threads(self, **kwargs):
            if False:
                yield None

        async def create_thread(self, *, name, embed, files, applied_tags, allowed_mentions):
            created.append((name, embed, [file.filename for file in files]))
            new_message.embeds = [embed]
            new_message.attachments = [SimpleNamespace(id=8, filename="capa.jpg"),
                                       SimpleNamespace(id=9, filename=files[-1].filename)]
            return SimpleNamespace(thread=thread, message=new_message)

    forum = Forum()
    monkeypatch.setattr(discord, "ForumChannel", Forum, raising=False)

    new_message = SimpleNamespace(id=30, channel=thread, author=SimpleNamespace(id=5), embeds=[], attachments=[])

    async def ready():
        pass

    async def raw(channel_id, message_id):
        assert (channel_id, message_id) == (40, 30)
        attachment = new_message.attachments[-1]
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
                "embeds": [new_message.embeds[0].to_dict()],
                "attachments": [{"id": str(attachment.id), "filename": attachment.filename,
                                 "url": f"https://cdn.discordapp.com/attachments/40/{attachment.id}/{attachment.filename}"}]}

    class Worker(ArchiveMixin):
        def __init__(self):
            self.client = SimpleNamespace(wait_until_ready=ready,
                                          get_channel=lambda channel_id: {2: channel, 20: forum}.get(channel_id),
                                          user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=raw))
            self.states = {}

        async def _archive_wait_stable_voice(self):
            pass

        async def _archive_audio_ready(self, audio, folder):
            output = folder / "audio.ogg"
            output.write_bytes(audio.read_bytes())
            return output, 254.08, 0, "opus"

        async def _archive_probe(self, audio):
            return {"streams": [{"codec_type": "audio", "index": 0, "bit_rate": "128000", "sample_rate": "48000"}]}

        async def _archive_cover(self, url, folder):
            output = folder / "capa.jpg"
            output.write_bytes(b"\xff\xd8\xff")
            return output

    async def instant(_):
        pass

    monkeypatch.setattr(ArchiveMixin._archive_one.__globals__["asyncio"], "sleep", instant)
    item = {"key": key, "guild_id": 1, "channel_id": 20, "emoji": "🎵",
            "existing_ref": {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4},
            "track": {"title": "Arctic Monkeys - 505", "source": "YouTube",
                      "webpage_url": original, "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg"}}
    worker = Worker()
    result = await worker._archive_one(item)
    assert result["status"] == "done" and result["presentation"] == 3
    assert result["reference"]["forum_id"] == 20
    assert result["reference"]["channel_id"] == 40
    assert result["reference"]["message_id"] == 30
    assert result["reference"]["attachment_id"] == 9
    assert created[0][0] == "Arctic Monkeys - 505"
    assert created[0][2] == ["capa.jpg", "Arctic Monkeys - 505.ogg"]
    assert new_message.embeds[0].thumbnail.url == "attachment://capa.jpg"
    assert message.attachments[0].id == 4  # migração só apaga depois de gravar o novo índice
    assert (await worker._archive_existing(forum, {**item, "retry": True})) == (new_message, 3)
    assert (await worker._archive_one({**item, "retry": True}))["reference"] == result["reference"]
    assert len(created) == 1


def test_troca_para_forum_preserva_audio_ate_novo_post_validado(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    track = _track()
    key = arquivo.media_key(track)
    arquivo.set_channel(1, 2)
    arquivo.record_play(track, "1")
    arquivo.record_play(track, "2")
    old = {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4}
    arquivo.mark_result(key, {"status": "done", "reference": old, "emoji": "🎵", "presentation": 2})
    arquivo.set_channel(1, 20, kind="forum")
    assert arquivo.archived(track)[0] == old
    assert arquivo.pending()["reference"] == old
    arquivo.mark_attempt(key)
    assert arquivo.pending()["retry"] is True  # reinício entre publicação e ACK
    new = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 3})
    assert arquivo.archived(track)[0] == new
    assert arquivo.pending() is None
    assert arquivo.cleanup_pending()["previous"] == old
    arquivo.mark_cleanup(key, old, done=True)
    assert arquivo.cleanup_pending() is None


@pytest.mark.asyncio
async def test_reproducao_do_post_le_emoji_e_qualidade_em_uma_mensagem():
    from types import SimpleNamespace
    from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin
    from cogs.musica.runtime_telefone.agente.estado import AgentTrack

    key = "a" * 32
    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    raw = {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
           "embeds": [{"url": _archive_url("https://youtu.be/abc", key, version=3),
                       "footer": {"text": "Arquivo de músicas"},
                       "fields": [{"name": "Fonte", "value": "🎵 YouTube"},
                                  {"name": "Duração", "value": "4:14"},
                                  {"name": "Formato", "value": "OPUS · OGG · 48 kHz"},
                                  {"name": "Qualidade", "value": "≈160 kbps"}] }],
           "attachments": [{"id": "9", "filename": "505.ogg",
                            "url": "https://cdn.discordapp.com/attachments/40/9/505.ogg?ex=ffffffff&hm=signed"}]}
    calls = []

    async def get_message(channel_id, message_id):
        calls.append((channel_id, message_id))
        return raw

    class Agent(ResolucaoMixin):
        client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))

        def _agent_track_from_metadata(self, track_meta, *, body):
            return AgentTrack(title="505", audio_abr=128)

    track = await Agent()._resolve_archive_attachment(track_meta={"archive_key": key, "archive_ref": ref}, body={"guild_id": 1})
    assert calls == [(40, 30)]
    assert track.archive_ref == ref and track.source_emoji == "🎵"
    assert track.audio_abr == 160 and track.audio_sample_rate == 48000
    assert track.stream_url == raw["attachments"][0]["url"]


@pytest.mark.asyncio
async def test_limpeza_so_remove_antigo_depois_de_validar_novo_e_liberar_fila():
    from types import SimpleNamespace
    from cogs.musica.runtime_telefone.agente.estado import AgentTrack

    discord = ArchiveMixin.cmd_archive_cleanup.__globals__["discord"]
    key = "a" * 32
    old = {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4}
    new = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    previous_embed = discord.Embed(url="https://youtu.be/abc")
    previous_embed.set_footer(text="music-archive:v1:" + key)
    removed = []

    async def delete():
        removed.append(True)

    message = SimpleNamespace(author=SimpleNamespace(id=5), embeds=[previous_embed],
                              attachments=[SimpleNamespace(id=4)], delete=delete)

    class Channel:
        async def fetch_message(self, message_id):
            assert message_id == 3
            return message

    async def get_message(channel_id, message_id):
        assert (channel_id, message_id) == (40, 30)
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
                "embeds": [{"url": _archive_url("https://youtu.be/abc", key, version=3),
                            "footer": {"text": "Arquivo de músicas"},
                            "fields": [{"name": "Fonte", "value": "🎵 YouTube"},
                                       {"name": "Duração", "value": "4:14"}]}],
                "attachments": [{"id": "9", "filename": "505.ogg",
                                 "url": "https://cdn.discordapp.com/attachments/40/9/505.ogg?ex=ffffffff&hm=signed"}]}

    class Agent(ArchiveMixin):
        client = SimpleNamespace(user=SimpleNamespace(id=5), get_channel=lambda _: Channel(),
                                 http=SimpleNamespace(get_message=get_message))

    agent = Agent()
    agent.states = {1: SimpleNamespace(current=None, queue=[AgentTrack(archive_ref=old)])}
    body = {"guild_id": 1, "archive_key": key, "archive_ref": new, "previous_ref": old,
            "track": {"webpage_url": "https://youtu.be/abc"}}
    assert (await agent.cmd_archive_cleanup(body))["removed"] is False
    assert not removed
    agent.states[1].queue = []
    assert (await agent.cmd_archive_cleanup(body))["removed"] is True
    assert removed == [True]
