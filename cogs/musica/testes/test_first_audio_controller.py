"""As esperas opcionais não podem atrasar o play nem enfraquecer a admissão."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.musica.interface import componentes as ui
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack, PlaylistCursor
from cogs.musica.reproducao.inicio import MusicStartTrace


def song(index=0):
    return MusicTrack(title=f"Faixa {index}", uploader="Artista", duration=180,
                      webpage_url=f"https://www.youtube.com/watch?v=abcdefghij{index}",
                      original_url=f"https://www.youtube.com/watch?v=abcdefghij{index}",
                      requester_id=8, source="YouTube", extractor="worker-ytdlp")


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from cogs.musica.busca import arquivo, memoria
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "learning.sqlite3")
    memoria.limpar_memoria_busca()
    arquivo.set_channel(1, 20, kind="forum")
    yield arquivo, memoria
    memoria.limpar_memoria_busca()


def environment(*, query="https://www.youtube.com/watch?v=abcdefghijk", ready=True):
    voice, text = SimpleNamespace(id=50), SimpleNamespace(id=60)
    state = SimpleNamespace(last_text_channel_id=60, current_lavalink_player=None)
    selection = SimpleNamespace(available=ready, message="Sistema de música indisponível no momento: teste")
    router = SimpleNamespace(
        music_worker_only_enabled=lambda: True,
        current_music_operation_generation=lambda guild: 0,
        get_state=lambda guild: state,
        ensure_music_worker_available=AsyncMock(return_value=selection),
        extractor=SimpleNamespace(extract=AsyncMock(side_effect=AssertionError("provedor indevido"))),
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, voice_client=None, get_channel=lambda channel: text),
        user=SimpleNamespace(id=8, display_name="Pessoa", voice=SimpleNamespace(channel=voice)),
        channel=text,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock(return_value=None)),
        edit_original_response=AsyncMock(), message=None,
    )
    return router, interaction


async def drain(router):
    while getattr(router, "_music_fast_start_tasks", None):
        await asyncio.gather(*list(router._music_fast_start_tasks))


@pytest.mark.asyncio
async def test_selection_dispatches_while_discord_reaction_is_blocked(storage, monkeypatch):
    router, interaction = environment()
    reaction_started, release_reaction, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    registered = []

    async def reaction(_emoji):
        reaction_started.set()
        await release_reaction.wait()

    interaction.message = SimpleNamespace(add_reaction=reaction, remove_reaction=AsyncMock())
    sent = []

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        dispatched.set()
        return {"ok": True, "cancelled": True}

    monkeypatch.setattr(ui, "music_agent_command", command)
    monkeypatch.setattr(ui, "registrar_selecao_busca", lambda *args, **kwargs: registered.append((args, kwargs)))
    select = ui.SearchSelect(router, 1, 50, 60, [song()], 8, "faixa",
                             search_timing_ms={"search_to_results_ms": 900})
    select._values = ["0"]
    task = asyncio.create_task(select.callback(interaction))
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        assert not release_reaction.is_set()
        assert registered and sent[0][0] == "play"
        body = sent[0][1]
        assert body["trace_id"] and body["start_context"]["entrypoint"] == "selection"
        assert body["controller_timing_ms"]["search_to_results_ms"] == 900
        assert body["controller_timing_ms"]["click_to_dispatch_ms"] >= 0
        router.ensure_music_worker_available.assert_awaited_once()
    finally:
        release_reaction.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["requester", "voice", "worker"])
async def test_selection_keeps_permission_voice_and_worker_checks(storage, monkeypatch, invalid):
    router, interaction = environment(ready=invalid != "worker")
    if invalid == "requester":
        interaction.user.id = 99
    elif invalid == "voice":
        interaction.user.voice.channel = None
    command = AsyncMock(side_effect=AssertionError("play não autorizado"))
    monkeypatch.setattr(ui, "music_agent_command", command)
    monkeypatch.setattr(ui, "registrar_selecao_busca", lambda *args, **kwargs: pytest.fail("escolha inválida gravada"))
    select = ui.SearchSelect(router, 1, 50, 60, [song()], 8, "faixa")
    select._values = ["0"]
    await select.callback(interaction)
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_modal_direct_url_does_not_resolve_before_play(storage, monkeypatch):
    router, interaction = environment()
    resolve = AsyncMock(side_effect=AssertionError("resolução duplicada"))
    monkeypatch.setattr(ui, "resolve_music_tracks_on_worker", resolve)
    sent = []

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        return {"ok": True, "cancelled": True}

    monkeypatch.setattr(ui, "music_agent_command", command)
    modal = ui.AddSongModal(router, 1, voice_channel_id=50, text_channel_id=60)
    modal.query._value = "https://www.youtube.com/watch?v=abcdefghijk"
    await modal.on_submit(interaction)
    await drain(router)
    play = [body for action, body in sent if action == "play"]
    assert len(play) == 1
    assert play[0]["track"]["stream_url"] == ""
    assert play[0]["start_context"]["entrypoint"] == "modal"
    assert play[0]["trace_id"] == play[0]["prepare_id"]
    resolve.assert_not_awaited()
    router.extractor.extract.assert_not_awaited()
    router.ensure_music_worker_available.assert_awaited_once()


@pytest.mark.asyncio
async def test_modal_uses_archived_spotify_link_and_keeps_guild_isolation(storage):
    archive, _memory = storage
    track = song()
    track.original_url = "https://open.spotify.com/track/knowntrack"
    archive.record_play(track, "play-1")
    reference = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    archive.mark_result(archive.media_key(track), {"status": "done", "reference": reference, "presentation": 8})
    router, _interaction = environment()
    batch, choice = await ui._extract_batch_for_add_modal(router, 1, track.original_url,
                                                         requester_id=8, requester_name="Pessoa")
    assert not choice and batch.tracks[0].title == track.title
    router.extractor.extract.assert_not_awaited()
    router.extractor.extract = AsyncMock(return_value=ExtractedBatch([song()], track.original_url))
    await ui._extract_batch_for_add_modal(router, 2, track.original_url,
                                         requester_id=8, requester_name="Pessoa")
    router.extractor.extract.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_modal_prepares_voice_during_metadata_and_cancels_owned_lease(storage, monkeypatch, stop):
    router, interaction = environment()
    started, release = asyncio.Event(), asyncio.Event()
    generation = [0]
    router.current_music_operation_generation = lambda guild: generation[0]
    sent = []

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        if action == "prepare_voice":
            started.set()
        return {"ok": True}

    async def metadata(*args, **kwargs):
        await asyncio.wait_for(started.wait(), timeout=1)
        await release.wait()
        if not stop:
            raise ValueError("metadados indisponíveis")
        return ExtractedBatch([song()], "metadata")

    router.extractor.extract = metadata
    monkeypatch.setattr(ui, "music_agent_command", command)
    modal = ui.AddSongModal(router, 1, voice_channel_id=50, text_channel_id=60)
    modal.query._value = "https://open.spotify.com/track/knowntrack"
    task = asyncio.create_task(modal.on_submit(interaction))
    await asyncio.wait_for(started.wait(), timeout=1)
    if stop:
        generation[0] += 1
    release.set()
    await task
    await drain(router)
    assert [action for action, _ in sent] == ["prepare_voice", "cancel_prepare_voice"]
    assert sent[0][1]["prepare_id"] == sent[1][1]["prepare_id"]


@pytest.mark.asyncio
async def test_modal_text_search_keeps_explicit_choice_without_voice(storage, monkeypatch):
    router, interaction = environment()
    monkeypatch.setattr(ui, "resolve_music_tracks_on_worker", AsyncMock(return_value=ExtractedBatch([song(), song(1)], "faixa")))
    monkeypatch.setattr(ui, "_schedule_agent_prefetch", lambda *args, **kwargs: None)
    command = AsyncMock(side_effect=AssertionError("voz antes da escolha"))
    monkeypatch.setattr(ui, "music_agent_command", command)
    modal = ui.AddSongModal(router, 1)
    modal.query._value = "faixa"
    await modal.on_submit(interaction)
    command.assert_not_awaited()
    view = interaction.followup.send.await_args.kwargs["view"]
    assert isinstance(view, ui.SearchResultView)
    assert view.children[0].search_timing_ms["search_to_results_ms"] >= 0


@pytest.mark.asyncio
async def test_modal_journals_full_playlist_before_sending_bounded_window(storage, monkeypatch):
    _archive, memory = storage
    router, interaction = environment()
    tracks = [song(index) for index in range(8)]
    for index, track in enumerate(tracks):
        track.original_url = f"https://open.spotify.com/track/track{index}"
    cursor = PlaylistCursor(provider="spotify", source_url="https://open.spotify.com/playlist/knownplaylist",
                            next_offset=8, total_tracks=100, exhausted=False)
    router.extractor.extract = AsyncMock(return_value=ExtractedBatch(
        tracks, cursor.source_url, is_playlist=True, playlist_cursor=cursor))
    monkeypatch.setattr(ui.config, "MUSIC_PLAYLIST_WINDOW_SIZE", 5)
    monkeypatch.setattr(ui, "_sync_agent_panel", AsyncMock())
    monkeypatch.setattr(ui, "schedule_playlist_refill_from_result", lambda *args: None)
    # Leave durable events pending, as if the process stopped after delivery.
    monkeypatch.setattr(ui, "schedule_deferred_learning", lambda router: None)
    sent = []

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        if action == "enqueue_many":
            with memory._abrir_db() as db:
                rows = db.execute("SELECT tracks_json FROM escolhas_link_pendentes").fetchall()
            assert sum(len(json.loads(row[0])) for row in rows) == 8
            return {"ok": True, "added": 5, "state": {"status": "playing", "confirmed_playing": True}}
        return {"ok": True}

    monkeypatch.setattr(ui, "music_agent_command", command)
    modal = ui.AddSongModal(router, 1)
    modal.query._value = cursor.source_url
    await modal.on_submit(interaction)
    await drain(router)
    body = next(body for action, body in sent if action == "enqueue_many")
    assert len(body["tracks"]) == 6  # five tracks and the virtual cursor
    assert body["tracks"][-1]["virtual_playlist_cursor"]["next_offset"] == 5
    assert memory.processar_lotes_link_pendentes() == 1


def test_trace_counts_failures_and_keeps_human_wait_outside_click_duration(caplog, monkeypatch):
    from cogs.musica.reproducao import inicio
    clock = [100.0]
    monkeypatch.setattr(inicio.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.INFO, logger=inicio.__name__):
        search = MusicStartTrace(1, entrypoint="command", input_kind="search")
        clock[0] += 1
        search.search_results()
        search.finish()
        clock[0] += 120  # human choice time
        click = MusicStartTrace(1, entrypoint="selection", input_kind="search",
                                 timing_ms={"search_to_results_ms": 1000})
        clock[0] += .2
        click.dispatch()
        click.finish("timeout")
        click.finish("failed")
    records = [json.loads(record.message.split("[music-start] ", 1)[1])
               for record in caplog.records if "[music-start] " in record.message]
    assert [r["event"] for r in records] == ["music_search_results", "music_start_attempt", "music_start_outcome"]
    assert records[-1]["outcome"] == "timeout"
    assert records[-1]["controller_timing_ms"]["click_to_dispatch_ms"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["cancel_before_start", "expired", "rejected", "confirmed", "accepted"])
async def test_deferred_delivery_observes_terminal_outcome_once(monkeypatch, caplog, completion):
    from cogs.musica.agente_telefone import comandos, roteamento
    from cogs.musica.reproducao import inicio

    monkeypatch.setattr(comandos, "_DEFERRED_TASKS", {})
    monkeypatch.setattr(comandos, "_DEFERRED_STATE", {})
    monkeypatch.setattr(roteamento, "_DESTINOS_GUILD", {})
    clock = [100.0]
    monkeypatch.setattr(comandos, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(comandos, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, current_task=asyncio.current_task,
        Task=asyncio.Task, CancelledError=asyncio.CancelledError, sleep=AsyncMock()))
    destination = roteamento.DestinoWorker("phone", "Telefone", "http://phone:8766", "test-token")
    monkeypatch.setattr(comandos, "_resolver_destino_retry", AsyncMock(return_value=destination))
    result = ({"ok": False, "error": "playback rejected"} if completion == "rejected"
              else {"ok": True, "state": {"confirmed_playing": completion == "confirmed"}})
    monkeypatch.setattr(comandos, "post_json_destino", AsyncMock(return_value=result))
    payload = {"action": "play", "guild_id": 1, "voice_channel_id": 50, "command_id": "stable",
               "trace_id": "deferred-trace", "controller_timing_ms": {"command_to_dispatch_ms": 80},
               "start_context": {"entrypoint": "command", "input_kind": "url"}}
    with caplog.at_level(logging.INFO, logger=inicio.__name__):
        comandos._agendar_comando_diferido(destino=destination, payload=payload,
            total_timeout=10, erro_inicial=ConnectionError("offline"))
        task = comandos._DEFERRED_TASKS[1]
        if completion == "cancel_before_start":
            comandos._cancelar_comando_diferido(1, reason="stop")
        elif completion == "expired":
            clock[0] += 1000
        await asyncio.gather(task, return_exceptions=True)
    records = [json.loads(record.message.split("[music-start] ", 1)[1])
               for record in caplog.records if "[music-start] " in record.message]
    if completion == "accepted":
        assert records == []  # an ACK alone does not establish first audio
    else:
        assert len(records) == 1
        expected = {"cancel_before_start": "cancelled", "expired": "timeout",
                    "rejected": "failed", "confirmed": "success"}[completion]
        assert records[0]["outcome"] == expected
        assert records[0]["trace_id"] == "deferred-trace"
