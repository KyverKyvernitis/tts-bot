from __future__ import annotations

import pytest

from cogs.musica.busca import arquivo
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.arquivo_coordenador import ArchiveCoordinator
from cogs.musica.runtime_telefone.agente.validade_stream import (
    ArchiveMixin, DiscordAttachmentError, _archive_audio_filename, _archive_cover_confirmed, _archive_metadata, _archive_url,
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
    # Os registros antigos de reprodução já pertenciam a escolhas aprendidas.
    assert arquivo.pending()["key"] == key
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
    with arquivo._db() as db:
        assert db.execute("SELECT reference_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone() == ("",)
    assert arquivo.pending() is not None


def test_aprendida_apos_uma_reproducao_elegivel_sem_segunda_vez(tmp_path, monkeypatch):
    from cogs.musica.busca import memoria

    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    memoria.limpar_memoria_busca()
    try:
        track = _track()
        arquivo.set_channel(1, 2, kind="forum")
        assert arquivo.record_play(track, "inicio-unico")
        assert arquivo.pending()["key"] == arquivo.media_key(track)
        assert memoria.registrar_selecao_busca("um título", track)
        assert arquivo.flush_learned() == 1
        assert arquivo.pending()["key"] == arquivo.media_key(track)
        assert arquivo.counts()["learned"] == 1
        with arquivo._db() as db:
            assert db.execute("SELECT tocadas, aprendida_em FROM arquivo_musicas").fetchone()[0] == 1
            assert db.execute("SELECT aprendida_em FROM arquivo_musicas").fetchone()[0] > 0
        # Os aliases para a consulta e para o título não geram posts duplicados.
        assert memoria.registrar_link_busca(_track(original="https://www.youtube.com/watch?v=abc123"))
        assert arquivo.flush_learned() == 1
        assert arquivo.counts()["learned"] == 1
    finally:
        memoria.limpar_memoria_busca()


def test_varredura_recupera_playlist_aprendida_antes_de_tocar(tmp_path, monkeypatch):
    from cogs.musica.busca import memoria

    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    memoria.limpar_memoria_busca()
    try:
        arquivo.set_channel(1, 2, kind="forum")
        first = _track(webpage="https://www.youtube.com/watch?v=abc123",
                       original="https://www.youtube.com/watch?v=abc123")
        second = _track(webpage="https://www.youtube.com/watch?v=xyz456",
                        original="https://www.youtube.com/watch?v=xyz456")
        second.title = "Outra música aprendida"
        assert memoria.registrar_lote_link_busca([first, second]) > 2
        # Simula reinício do bot antes de escoar o buffer de notificações.
        with arquivo._learned_lock:
            arquivo._new_learned.clear()
        cursor = ""
        while True:
            new_cursor, page = arquivo.learned_page(cursor, limit=2)
            if new_cursor == cursor:
                break
            arquivo.register_learned_batch(page)
            cursor = new_cursor
        assert arquivo.counts()["learned"] == 2
        assert arquivo.pending()["key"] in {arquivo.media_key(first), arquivo.media_key(second)}
        with arquivo._db() as db:
            assert db.execute("SELECT MIN(tocadas), MAX(tocadas) FROM arquivo_musicas").fetchone() == (0, 0)
    finally:
        memoria.limpar_memoria_busca()


@pytest.mark.asyncio
async def test_varredura_continua_recupera_escolha_perdida_do_buffer(tmp_path, monkeypatch):
    import asyncio
    from cogs.musica.busca import memoria
    from cogs.musica import arquivo_coordenador as coordinator_module

    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    memoria.limpar_memoria_busca()
    try:
        arquivo.set_channel(1, 2, kind="forum")
        first = _track()
        second = _track(webpage="https://www.youtube.com/watch?v=xyz456")
        second.title = "Outra faixa"
        memoria.registrar_selecao_busca("minha escolha", first, now=100.0)
        with arquivo._learned_lock:
            arquivo._new_learned.clear()

        class Bot:
            async def wait_until_ready(self):
                pass

        manager = ArchiveCoordinator(Bot())
        original_sleep = asyncio.sleep
        passes = 0

        async def finish_after_incremental(seconds):
            nonlocal passes
            if seconds == 12:
                passes += 1
                if passes == 1:
                    # Alterar um alias já paginado e perder o aviso rápido
                    # deve continuar recuperável sem reiniciar o bot.
                    memoria.registrar_selecao_busca("minha escolha", second, now=101.0)
                    with arquivo._learned_lock:
                        arquivo._new_learned.clear()
                else:
                    raise asyncio.CancelledError
            else:
                await original_sleep(0)

        monkeypatch.setattr(coordinator_module.asyncio, "sleep", finish_after_incremental)
        with pytest.raises(asyncio.CancelledError):
            await manager._seed_learned()
        assert passes == 2
        assert arquivo.counts()["learned"] == 2
        assert arquivo.pending()["key"] in {arquivo.media_key(first), arquivo.media_key(second)}
    finally:
        memoria.limpar_memoria_busca()


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


@pytest.mark.asyncio
async def test_arquivo_espera_novo_agente_antes_de_enviar_capa(tmp_path, monkeypatch):
    import asyncio
    from cogs.musica import arquivo_coordenador as coordinator_module

    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    track = _track()
    key = arquivo.media_key(track)
    arquivo.set_channel(1, 2, kind="forum")
    arquivo.record_play(track, "primeiro")
    arquivo.record_play(track, "segundo")

    class Bot:
        async def wait_until_ready(self):
            pass

    manager = ArchiveCoordinator(Bot())
    version = "0.3.76"
    calls = []
    real_pending = arquivo.pending
    real_sleep = asyncio.sleep

    async def status_fake(**kwargs):
        assert kwargs["guild_id"] == 1
        return {"available": True, "version": version}

    async def command_fake(action, **kwargs):
        calls.append(action)
        if action == "archive_enqueue":
            return {"ok": True, "status": "queued"}
        return {"ok": True, "status": "done", "presentation": 7,
                "reference": {"guild_id": 1, "forum_id": 2, "channel_id": 3,
                              "message_id": 4, "attachment_id": 5}}

    async def fast_sleep(seconds):
        if seconds == 12:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(coordinator_module, "music_agent_status", status_fake)
    monkeypatch.setattr(coordinator_module, "music_agent_command", command_fake)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await manager._run()
    assert calls == []
    assert real_pending()["key"] == key

    version = "0.3.77"
    pending_calls = 0

    def pending_once(**kwargs):
        nonlocal pending_calls
        pending_calls += 1
        if pending_calls > 1:
            raise asyncio.CancelledError
        return real_pending(**kwargs)

    monkeypatch.setattr(arquivo, "pending", pending_once)
    with pytest.raises(asyncio.CancelledError):
        await manager._run()
    assert calls == ["archive_enqueue", "archive_status"]
    with arquivo._db() as db:
        assert db.execute("SELECT estado, apresentacao FROM arquivo_musicas WHERE chave=?", (key,)).fetchone() == ("done", 7)


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


def test_novas_aprendidas_passam_na_frente_mas_posts_antigos_tambem_progridem(tmp_path, monkeypatch):
    from cogs.musica.busca.memoria import _track_payload

    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    arquivo.set_channel(1, 2, kind="forum")
    old = _track(webpage="https://www.youtube.com/watch?v=old")
    new = _track(webpage="https://www.youtube.com/watch?v=new")
    arquivo.record_play(old, "legado-aprendido")
    old_key = arquivo.media_key(old)
    old_ref = {"guild_id": 1, "forum_id": 2, "channel_id": 3, "message_id": 4, "attachment_id": 5}
    arquivo.mark_result(old_key, {"status": "done", "reference": old_ref, "presentation": 6})
    assert arquivo.register_learned_batch([(_track_payload(new), 123.0)]) == 1
    assert arquivo.pending()["key"] == arquivo.media_key(new)
    with arquivo._db() as db:
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE chave=?", (old_key,))
    assert arquivo.pending(prefer_new=False)["key"] == old_key
    assert arquivo.counts()["unposted"] == 1


def test_upgrade_libera_reparo_v5_imediatamente(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "memoria.sqlite3")
    arquivo.set_channel(1, 2, kind="forum")
    track = _track()
    arquivo.record_play(track, "legado-aprendido")
    key = arquivo.media_key(track)
    ref = {"guild_id": 1, "forum_id": 2, "channel_id": 3, "message_id": 4, "attachment_id": 5}
    arquivo.mark_result(key, {"status": "done", "reference": ref, "presentation": 5})
    with arquivo._db() as db:
        assert db.execute("SELECT tentativa_em FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()[0] > 0
        db.execute("PRAGMA user_version=4")
    assert arquivo.pending()["reference"] == ref


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
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key, version=4)
    assert _archive_metadata(raw, key, 5, {**ref, "forum_id": 7})["duration"] == 235.0
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key, version=5)
    assert _archive_metadata(raw, key, 5, {**ref, "forum_id": 7})["duration"] == 235.0
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key, version=6)
    assert _archive_metadata(raw, key, 5, {**ref, "forum_id": 7})["duration"] == 235.0
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key, version=7)
    raw["embeds"][0]["description"] = "<:YouTube:123> YouTube • 3:55\nOPUS · OGG · 48 kHz • ≈128 kbps"
    raw["embeds"][0]["fields"] = []
    compact = _archive_metadata(raw, key, 5, {**ref, "forum_id": 7})
    assert (compact["emoji"], compact["source"], compact["duration"]) == ("<:YouTube:123>", "YouTube", 235)
    assert compact["audio_abr"] == 128 and compact["audio_sample_rate"] == 48000
    raw["attachments"].extend((
        {"id": "6", "filename": "quebrada.jpg", "url": "https://example.com/invalid.jpg"},
        {"id": "7", "filename": "capa.jpg", "url": "https://cdn.discordapp.com/attachments/2/7/capa.jpg"},
    ))
    assert _archive_cover_confirmed(raw)
    raw["attachments"] = raw["attachments"][:1]
    raw["embeds"][0]["url"] = _archive_url("https://www.youtube.com/watch?v=abc", key, version=6)
    raw["embeds"][0]["fields"] = [{"name": "Fonte", "value": "<:YouTube:123> YouTube"},
                                  {"name": "Duração", "value": "3:55"}]
    raw["embeds"][0]["thumbnail"] = {"url": "attachment://capa.jpg"}
    raw["attachments"].append({"id": "7", "filename": "capa.jpg",
                                 "url": "https://cdn.discordapp.com/attachments/2/7/capa.jpg?ex=ffff&hm=abc"})
    assert not _archive_cover_confirmed(raw)
    raw["embeds"][0]["thumbnail"]["url"] = "https://cdn.discordapp.com/attachments/2/7/capa.jpg"
    assert _archive_cover_confirmed(raw)
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
async def test_monitor_nao_conta_reproducoes_para_arquivar_aprendidas(monkeypatch):
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

    assert calls >= 3
    assert observed == []


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
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "default=nw=1", str(jpeg)],
                            check=True, capture_output=True, text=True)
    assert "pix_fmt=yuvj420p" in result.stdout

    complete = tmp_path / "completa.jpg"
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=768x768:d=1", "-frames:v", "1", "-q:v", "3", "-y", str(complete)],
                   check=True, capture_output=True)
    assert await worker._archive_jpeg(complete, tmp_path / "capa-integra.jpg") is not None
    data = complete.read_bytes()
    broken = tmp_path / "capa-corrompida-mas-com-eoi.jpg"
    broken.write_bytes(data[:len(data) // 2] + b"\xff\xd9")
    # O FFmpeg, sem verificação estrita, retorna sucesso e pinta de verde a
    # metade ausente, mesmo com um marcador JPEG de fim presente.
    assert await worker._archive_jpeg(broken, tmp_path / "capa-rejeitada.jpg") is None


@pytest.mark.asyncio
async def test_capa_tenta_outro_link_e_fallback_youtube(tmp_path):
    calls = []

    class Worker(ArchiveMixin):
        async def _archive_cover(self, url, folder):
            calls.append(url)
            return folder / "capa.jpg" if url.endswith("/hqdefault.jpg") else None

    worker = Worker()
    result = await worker._archive_cover_for_track({
        "display_thumbnail": "https://i.scdn.co/image/indisponivel",
        "thumbnail": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
    }, tmp_path)
    assert result == tmp_path / "capa.jpg"
    assert calls == ["https://i.scdn.co/image/indisponivel", "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
                     "https://i.ytimg.com/vi/abc/hqdefault.jpg"]


@pytest.mark.asyncio
async def test_capa_le_todos_os_blocos_e_rejeita_jpeg_incompleto(tmp_path, monkeypatch):
    aiohttp = ArchiveMixin._archive_cover.__globals__["aiohttp"]
    chunks = [b"\xff\xd8\xffprimeiro", b"restante", b"\xff\xd9"]
    converted = []

    class Response:
        status = 200
        headers = {"Content-Type": "image/jpeg"}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            assert size > 0
            for chunk in chunks:
                yield chunk

        async def read(self, size):
            raise AssertionError("read(n) poderia entregar só o primeiro bloco")

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert url.startswith("https://i.ytimg.com/")
            return Response()

    class Worker(ArchiveMixin):
        async def _archive_jpeg(self, source, output):
            converted.append(source.read_bytes())
            return output

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    worker = Worker()
    url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    assert await worker._archive_cover(url, tmp_path) == tmp_path / "capa.jpg"
    assert converted == [b"".join(chunks)]

    chunks.pop()  # origem cortada no meio do JPEG: FFmpeg a pintaria de verde
    assert await worker._archive_cover(url, tmp_path) is None
    assert len(converted) == 1

    chunks[:] = [b"\xff\xd8\xff", b"x" * (2 * 1024 * 1024), b"\xff\xd9"]
    assert await worker._archive_cover(url, tmp_path) is None
    assert len(converted) == 1


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
    edits = []

    class Thread:
        id = 40
        name = "Arctic Monkeys - 505"
        owner_id = 5

        async def history(self, **kwargs):
            yield new_message

        async def fetch_message(self, message_id):
            assert message_id == 30
            return new_message

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

        async def create_thread(self, *, name, content, embed, files, applied_tags, allowed_mentions):
            created.append((name, embed, [file.filename for file in files]))
            new_message.embeds = [embed]
            new_message.content = content or ""
            new_message.attachments = [SimpleNamespace(id=8, filename="capa.jpg",
                                                       url="https://cdn.discordapp.com/attachments/40/8/capa.jpg?ex=ffff&hm=abc"),
                                       SimpleNamespace(id=9, filename=files[-1].filename,
                                                       url=f"https://cdn.discordapp.com/attachments/40/9/{files[-1].filename}")]
            return SimpleNamespace(thread=thread, message=new_message)

    forum = Forum()
    monkeypatch.setattr(discord, "ForumChannel", Forum, raising=False)

    async def edit_post(*, embed, attachments, allowed_mentions, content=None):
        edits.append((embed, [attachment.id if hasattr(attachment, "id") else attachment.filename
                                  for attachment in attachments]))
        new_message.embeds = [embed]
        if content is not None:
            new_message.content = content
        new_message.attachments = [attachment if hasattr(attachment, "id") else SimpleNamespace(
            id=10, filename=attachment.filename,
            url="https://cdn.discordapp.com/attachments/40/10/capa-v7.jpg?ex=ffff&hm=abc")
            for attachment in attachments]
        return new_message

    new_message = SimpleNamespace(id=30, channel=thread, author=SimpleNamespace(id=5), embeds=[], content="",
                                  attachments=[], edit=edit_post)

    async def ready():
        pass

    async def raw(channel_id, message_id):
        assert (channel_id, message_id) == (40, 30)
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
                "embeds": [new_message.embeds[0].to_dict()],
                "attachments": [{"id": str(attachment.id), "filename": attachment.filename,
                                 "url": attachment.url} for attachment in new_message.attachments]}

    class Worker(ArchiveMixin):
        def __init__(self):
            self.client = SimpleNamespace(wait_until_ready=ready,
                                          get_channel=lambda channel_id: {2: channel, 20: forum, 40: thread}.get(channel_id),
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

        def log(self, *args, **kwargs):
            pass

    async def instant(_):
        pass

    monkeypatch.setattr(ArchiveMixin._archive_one.__globals__["asyncio"], "sleep", instant)
    item = {"key": key, "guild_id": 1, "channel_id": 20, "emoji": "🎵",
            "existing_ref": {"guild_id": 1, "channel_id": 2, "message_id": 3, "attachment_id": 4},
            "track": {"title": "Arctic Monkeys - 505", "source": "YouTube",
                      "webpage_url": original, "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg"}}
    worker = Worker()
    result = await worker._archive_one(item)
    assert result["status"] == "done" and result["presentation"] == 7
    assert result["reference"]["forum_id"] == 20
    assert result["reference"]["channel_id"] == 40
    assert result["reference"]["message_id"] == 30
    assert result["reference"]["attachment_id"] == 9
    assert created[0][0] == "Arctic Monkeys - 505"
    assert created[0][2] == ["capa.jpg", "Arctic Monkeys - 505.ogg"]
    assert not new_message.embeds[0].thumbnail.url
    assert new_message.content.startswith("🎵 YouTube ·")
    assert not edits  # novo post já nasce compacto, sem REST edit adicional
    assert message.attachments[0].id == 4  # migração só apaga depois de gravar o novo índice
    assert (await worker._archive_existing(forum, {**item, "retry": True})) == (new_message, 7)
    assert (await worker._archive_one({**item, "retry": True}))["reference"] == result["reference"]
    assert len(created) == 1
    assert not edits

    # Um post v6 com imagem íntegra só troca o layout; mantém os dois anexos.
    v6_embed = discord.Embed(title="Arctic Monkeys - 505", url=_archive_url(original, key, version=6))
    for name, value in (("Fonte", "🎵 YouTube"), ("Duração", "4:14"),
                        ("Formato", "OPUS · OGG · 48 kHz"), ("Qualidade", "≈128 kbps")):
        v6_embed.add_field(name=name, value=value)
    v6_embed.set_footer(text="Arquivo de músicas")
    v6_embed.set_thumbnail(url=new_message.attachments[0].url)
    new_message.embeds = [v6_embed]

    async def cover_must_not_download(url, folder):
        raise AssertionError("layout v6 com capa válida não deve baixar a imagem")

    worker._archive_cover = cover_must_not_download
    updated = await worker._archive_one({**item, "existing_ref": result["reference"]})
    assert updated["presentation"] == 7 and updated["reference"] == result["reference"]
    assert len(created) == 1 and len(edits) == 1 and edits[0][1] == [8, 9]
    worker._archive_cover = Worker._archive_cover.__get__(worker)

    # O índice v5 provoca reparo no mesmo post, sem baixar nem reenviar o áudio.
    def previous_embed():
        embed = discord.Embed(title="Arctic Monkeys - 505", url=_archive_url(original, key, version=5))
        embed.add_field(name="Fonte", value="🎵 YouTube")
        embed.add_field(name="Duração", value="4:14")
        embed.add_field(name="Formato", value="OPUS · OGG · 48 kHz")
        embed.add_field(name="Qualidade", value="≈128 kbps")
        embed.set_footer(text="Arquivo de músicas")
        embed.set_thumbnail(url="attachment://capa.jpg")
        return embed

    new_message.embeds = [previous_embed()]
    repaired = await worker._archive_one({**item, "existing_ref": result["reference"]})
    assert repaired["reference"] == result["reference"] and repaired["presentation"] == 7
    assert len(created) == 1 and len(edits) == 3
    assert edits[1][1] == [9, "capa-v7.jpg"] and edits[2][1] == [9, 10]
    assert not new_message.embeds[0].thumbnail.url

    # Sem origem válida, a imagem antiga (possivelmente verde) não deve voltar.
    async def missing_cover(url, folder):
        return None

    worker._archive_cover = missing_cover
    new_message.embeds = [previous_embed()]
    delayed = await worker._archive_one({**item, "existing_ref": result["reference"]})
    # O índice v5 aponta para capa.jpg, já substituída pelo reparo anterior.
    assert delayed["presentation"] == 3 and delayed["reference"] == result["reference"]
    assert len(edits) == 3


@pytest.mark.asyncio
async def test_playlist_spotify_arquiva_audio_da_fonte_resolvida(tmp_path, monkeypatch):
    from types import SimpleNamespace

    module = ArchiveMixin._archive_download.__globals__
    process_calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn(*cmd, **kwargs):
        process_calls.append(cmd)
        (tmp_path / "audio.ogg").write_bytes(b"opus")
        return Process()

    monkeypatch.setattr(module["asyncio"], "create_subprocess_exec", spawn)

    class Worker(ArchiveMixin):
        states = {}
        cookies_file = ""
        js_runtimes = ""
        _archive_active = "a" * 32

        def _query_from_track_meta(self, track):
            return "ytsearch1:Artista - Faixa official audio"

        async def resolve_track(self, query, **kwargs):
            assert query.startswith("ytsearch1:")
            assert kwargs["priority"] == 20
            return SimpleNamespace(webpage_url="https://www.youtube.com/watch?v=abc123",
                                   duration=183, source="YouTube")

    item = {"key": "a" * 32, "guild_id": 1, "emoji": "🎵",
            "track": {"title": "Artista - Faixa", "webpage_url": "https://open.spotify.com/track/abc123",
                      "source": "Spotify", "display_source": "Spotify", "duration": 181}}
    audio = await Worker()._archive_download(item, tmp_path)
    assert audio == tmp_path / "audio.ogg"
    assert len(process_calls) == 1 and process_calls[0][-1] == "https://www.youtube.com/watch?v=abc123"
    assert item["track"]["display_source"] == "YouTube"
    from cogs.musica import configuracao
    assert item["emoji"] == configuracao.MUSIC_SOURCE_EMOJIS["youtube"]


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
    assert arquivo.counts()["refresh"] == 1
    with arquivo._db() as db:
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE chave=?", (key,))
    assert arquivo.pending()["reference"] == new
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 4})
    assert arquivo.counts()["refresh"] == 1
    with arquivo._db() as db:
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE chave=?", (key,))
    assert arquivo.pending()["reference"] == new
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 5})
    assert arquivo.counts()["refresh"] == 1
    with arquivo._db() as db:
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE chave=?", (key,))
    assert arquivo.pending()["reference"] == new
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 6})
    assert arquivo.counts()["refresh"] == 1
    with arquivo._db() as db:
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE chave=?", (key,))
    arquivo.mark_result(key, {"status": "done", "reference": new, "emoji": "🎵", "presentation": 7})
    assert arquivo.counts()["refresh"] == 0
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
async def test_audio_do_forum_reutiliza_url_assinada_e_renova_apos_falha():
    import asyncio
    import time
    from types import SimpleNamespace
    from cogs.musica.runtime_telefone.agente.servidor import MusicAgent

    agent = MusicAgent()
    key = "a" * 32
    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    expiry = format(int(time.time() + 300), "x")
    calls = []

    async def get_message(channel_id, message_id):
        assert (channel_id, message_id) == (40, 30)
        calls.append(True)
        await asyncio.sleep(0)
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
                "embeds": [{"url": _archive_url("https://youtu.be/abc", key, version=7),
                            "footer": {"text": "Arquivo de músicas"},
                            "description": "<:YT:123> YouTube • 3:55\nOPUS · OGG · 48 kHz • ≈128 kbps"}],
                "attachments": [{"id": "9", "filename": "faixa.ogg", "url":
                                  f"https://cdn.discordapp.com/attachments/40/9/faixa.ogg?ex={expiry}&hm=sign{len(calls)}"}]}

    agent.client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))
    metadata = {"title": "Faixa", "archive_key": key, "archive_ref": ref}
    first, second = await asyncio.gather(*(agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1})
                                           for _ in range(2)))
    assert len(calls) == 1
    assert first.stream_url == second.stream_url and first.source_emoji == "<:YT:123>"
    assert first.audio_abr == 128 and first.audio_sample_rate == 48000
    refreshed = await agent._resolve_discord_attachment(track_meta=metadata, body={"guild_id": 1}, force_refresh=True)
    assert len(calls) == 2 and refreshed.stream_url != first.stream_url
    agent._invalidate_track_stream_cache(refreshed)
    another = await agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1})
    assert len(calls) == 3 and another.stream_url != refreshed.stream_url
    with pytest.raises(DiscordAttachmentError):
        await agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 2})
    assert len(calls) == 3


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
