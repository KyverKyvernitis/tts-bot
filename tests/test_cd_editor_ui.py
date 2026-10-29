"""Modern modal serialization, owner authorization and editor lifecycle."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.http import handle_message_parameters
import pytest

from cogs.tts.interface import editar_cd as ui
from cogs.tts.interface import painel_canto
from osaka_cd import discord_edit
from osaka_cd.discord_import import ImportCDError


def _rows(count=1):
    return tuple({'id': f'song-{index}', 'name': f'Song {index}', 'artist': 'Composer',
        'video_url': 'https://www.youtube.com/watch?v=kbNdx0yqbZE',
        'source_url': '', 'version': f'{index:064x}', 'notes': 50, 'duration_ms': 10000}
        for index in range(count))


def _context(monkeypatch, *, rows=None, owner=True):
    response = SimpleNamespace(id=7, edit=AsyncMock())
    message = SimpleNamespace(id=8, author=SimpleNamespace(id=42), attachments=[],
        channel=SimpleNamespace(send=AsyncMock(return_value=response)))
    tasks = []
    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task
    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
        _schedule_tts_background=schedule)
    service = SimpleNamespace(list_editable_cds=Mock(return_value=_rows() if rows is None else rows),
        edit_cd=AsyncMock(return_value=SimpleNamespace(state='activated')),
        parse_video_url=discord_edit.parse_video_url)
    monkeypatch.setattr(ui, '_service', lambda: service)
    return cog, message, response, tasks, service


def _interaction(*, owner=42, values=None):
    return SimpleNamespace(user=SimpleNamespace(id=owner),
        data={'values': values} if values is not None else {},
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()))


async def _open_modal(cog):
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    await session.view.choose(_interaction(values=['0']))
    interaction = _interaction()
    await session.view.edit_cd(interaction)
    return session, interaction.response.send_modal.await_args.args[0]


def _text(view):
    return json.dumps(view.to_components(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_editor_blocks_non_owner_and_busy_before_reading_catalog(monkeypatch):
    cog, message, _, tasks, service = _context(monkeypatch, owner=False)
    assert await ui.handle_edit_cd(cog, message)
    service.list_editable_cds.assert_not_called()
    assert not tasks and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    assert 'Só o dono' in _text(message.channel.send.await_args.kwargs['view'])
    cog.bot.is_owner.return_value = True
    lease = object()
    setattr(cog.bot, ui._LEASE_ATTRIBUTE, lease)
    assert await ui.handle_edit_cd(cog, message)
    service.list_editable_cds.assert_not_called()
    assert getattr(cog.bot, ui._LEASE_ATTRIBUTE) is lease


@pytest.mark.asyncio
async def test_empty_catalog_and_failed_open_release_global_lease(monkeypatch):
    cog, message, response, _, service = _context(monkeypatch, rows=())
    assert await ui.handle_edit_cd(cog, message)
    assert 'catálogo está vazio' in _text(response.edit.await_args.kwargs['view'])
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    service.list_editable_cds.side_effect = OSError('private failure')
    assert await ui.handle_edit_cd(cog, message)
    assert 'private failure' not in _text(response.edit.await_args.kwargs['view'])
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_actual_v2_picker_and_new_label_modal_have_valid_payloads(monkeypatch):
    cog, message, response, _, _ = _context(monkeypatch)
    assert await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    for call in [message.channel.send.await_args, *response.edit.await_args_list]:
        with handle_message_parameters(**call.kwargs) as request:
            payload = request.payload
        assert payload['components'][0]['type'] == 17
        assert discord.MessageFlags._from_value(payload['flags']).components_v2
        assert payload['allowed_mentions']['parse'] == []
        assert not payload.get('embeds')
    view = session.view.to_components()[0]
    assert any(item['type'] == 9 and item['accessory']['type'] == 11 for item in view['components'])
    components = modal.to_dict()['components']
    assert [item['type'] for item in components] == [18, 18, 18]
    assert all(item['component']['type'] == 4 for item in components)
    assert not any('description' in item for item in components)
    assert not any(item['type'] == 1 for item in components)
    content = _text(session.view)
    assert 'Escolha um CD para editar o nome' not in content
    assert 'só o dono do bot pode editar' not in content
    assert modal.video.default == 'https://www.youtube.com/watch?v=kbNdx0yqbZE'
    await session.view.close(_interaction())
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_pagination_limits_options_and_rejects_forged_other_page_selection(monkeypatch):
    cog, message, _, _, _ = _context(monkeypatch, rows=_rows(51))
    await ui.handle_edit_cd(cog, message)
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    assert len(session.view.select.options) == 25
    invalid = _interaction(values=['50'])
    await session.view.choose(invalid)
    assert not session.selected
    invalid.response.send_message.assert_awaited_once()
    await session.view.following(_interaction())
    assert session.page == 1 and len(session.view.select.options) == 25
    await session.view.following(_interaction())
    assert session.page == 2 and len(session.view.select.options) == 1
    await session.view.choose(_interaction(values=['50']))
    assert session.selected == 'song-50'
    await session.view.on_timeout()
    assert session.finished and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_owner_modal_saves_exact_url_and_bound_version_once_then_refreshes(monkeypatch):
    cog, message, response, tasks, service = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    modal.name._value = 'Monitoring'
    modal.artist._value = 'DECO*27'
    modal.video._value = 'https://youtu.be/kbNdx0yqbZE?si=tracking'
    service.list_editable_cds.return_value = (dict(_rows()[0], name='Monitoring', version='f' * 64),)
    await asyncio.gather(modal.on_submit(_interaction()), modal.on_submit(_interaction()))
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    service.edit_cd.assert_awaited_once()
    kwargs = service.edit_cd.await_args.kwargs
    assert kwargs['title'] == 'Monitoring' and kwargs['artist'] == 'DECO*27'
    assert kwargs['video_url'] == 'https://youtu.be/kbNdx0yqbZE?si=tracking'
    assert kwargs['expected_version'] == '0' * 64
    assert service.edit_cd.await_args.args == ('song-0',)
    assert session.rows[0]['version'] == 'f' * 64
    assert 'CD atualizado' in _text(response.edit.await_args.kwargs['view'])
    assert session.waiting and not session.progress_active
    await session.view.close(_interaction())


@pytest.mark.asyncio
async def test_bad_url_other_user_and_stale_form_never_reach_edit_service(monkeypatch):
    cog, message, _, tasks, service = _context(monkeypatch, rows=_rows(2))
    await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    modal.video._value = 'https://127.0.0.1/private'
    invalid = _interaction()
    await modal.on_submit(invalid)
    invalid.response.send_message.assert_awaited_once()
    assert not session.submitted and not tasks
    modal.video._value = 'https://youtu.be/kbNdx0yqbZE'
    other = _interaction(owner=999)
    await modal.on_submit(other)
    assert not tasks
    await session.view.choose(_interaction(values=['1']))
    stale = _interaction()
    await modal.on_submit(stale)
    assert 'Editor atualizado' in _text(stale.response.send_message.await_args.kwargs['view'])
    service.edit_cd.assert_not_awaited()
    await session.view.close(_interaction())


@pytest.mark.asyncio
async def test_conflict_refreshes_form_version_and_keeps_editor_available(monkeypatch):
    cog, message, response, tasks, service = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    service.edit_cd.side_effect = ImportCDError('conflict')
    service.list_editable_cds.return_value = (dict(_rows()[0], version='f' * 64),)
    await modal.on_submit(_interaction())
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert session.waiting and session.rows[0]['version'] == 'f' * 64
    assert 'mudou' in _text(response.edit.await_args.kwargs['view'])
    await session.view.close(_interaction())


@pytest.mark.asyncio
async def test_save_task_cancelled_before_start_releases_editor_lease(monkeypatch):
    cog, message, _, tasks, service = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    # Keep acknowledgement immediate so cancellation reaches the pre-start edge.
    monkeypatch.setattr(ui, '_defer', AsyncMock())
    await modal.on_submit(_interaction())
    tasks[0].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    assert not session.progress_active
    service.edit_cd.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['save', 'close'])
async def test_cancelled_interaction_ack_does_not_leave_editor_reserved(monkeypatch, operation):
    cog, message, _, tasks, service = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session, modal = await _open_modal(cog)
    monkeypatch.setattr(ui, '_defer', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        if operation == 'save':
            await modal.on_submit(_interaction())
        else:
            await session.view.close(_interaction())
    assert not tasks
    assert session.finished and not hasattr(cog.bot, ui._LEASE_ATTRIBUTE)
    service.edit_cd.assert_not_awaited()


@pytest.mark.asyncio
async def test_editor_reuses_regular_cd_registry_and_refreshes_missing_song_emoji(monkeypatch):
    known = {}
    registry = SimpleNamespace(emoji=Mock(side_effect=lambda identity: known.get(identity)), schedule=Mock())
    monkeypatch.setattr(painel_canto, '_CD_EMOJIS', registry)
    cog, message, response, _, _ = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    view = session.view
    registry.schedule.assert_called_with(cog.bot, view)
    registry.emoji.assert_called_with('song-0')
    assert view.select.options[0].emoji is None
    known['song-0'] = discord.PartialEmoji(name='osakacd_song_0', id=1559374199133315122)
    calls = response.edit.await_count
    await view.refresh(force=True)
    assert await view._edit_panel()
    assert response.edit.await_count == calls + 1
    assert view.select.options[0].emoji is known['song-0']
    assert not await view._edit_panel()  # No duplicate network edit for the same emoji.
    await view.close(_interaction())


@pytest.mark.asyncio
async def test_emoji_update_checks_current_editor_after_waiting_for_message_lock(monkeypatch):
    known = {}
    registry = SimpleNamespace(emoji=lambda identity: known.get(identity), schedule=Mock())
    monkeypatch.setattr(painel_canto, '_CD_EMOJIS', registry)
    cog, message, response, _, _ = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    view = session.view
    known['song-0'] = discord.PartialEmoji(name='osakacd_song_0', id=1559374199133315122)
    response.edit.reset_mock()
    await session.edit_lock.acquire()
    refresh = asyncio.create_task(view._edit_panel())
    await asyncio.sleep(0)
    session.release()
    session.edit_lock.release()
    assert not await refresh
    response.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_emoji_arriving_after_commit_updates_waiting_picker_before_task_finishes(monkeypatch):
    known = {}
    registry = SimpleNamespace(emoji=lambda identity: known.get(identity), schedule=Mock())
    monkeypatch.setattr(painel_canto, '_CD_EMOJIS', registry)
    cog, message, response, _, _ = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    view = session.view
    known['song-0'] = discord.PartialEmoji(name='osakacd_song_0', id=1559374199133315122)
    response.edit.reset_mock()
    task = asyncio.create_task(asyncio.sleep(.01))
    session.save_task = task
    # The service is already committed and this picker is waiting; a task's
    # pending return/done callback must not discard the only emoji notification.
    assert await view._edit_panel()
    assert response.edit.await_count == 1
    await task
    session.save_task = None
    assert view.select.options[0].emoji is known['song-0']
    assert not session.progress_active
    await view.close(_interaction())


@pytest.mark.asyncio
async def test_late_emoji_never_stops_or_overwrites_active_save_progress(monkeypatch):
    known = {}
    registry = SimpleNamespace(emoji=lambda identity: known.get(identity), schedule=Mock())
    monkeypatch.setattr(painel_canto, '_CD_EMOJIS', registry)
    cog, message, response, _, _ = _context(monkeypatch)
    await ui.handle_edit_cd(cog, message)
    session = getattr(cog.bot, ui._LEASE_ATTRIBUTE)
    view = session.view
    known['song-0'] = discord.PartialEmoji(name='osakacd_song_0', id=1559374199133315122)
    response.edit.reset_mock()
    session.waiting, session.submitted, session.progress_active = False, True, True
    await view.refresh(force=True)
    assert not await view._edit_panel()
    response.edit.assert_not_awaited()
    assert session.progress_active
    assert view.select.options[0].emoji is None
    session.release()
