from __future__ import annotations

import pytest

from cogs.musica.busca import arquivo
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.arquivo_coordenador import ArchiveCoordinator
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, DiscordAttachmentError, _archive_metadata


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
