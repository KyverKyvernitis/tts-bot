"""An idle editor is reusable UI; only actual CD work holds the busy lease."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from cogs.tts.interface import importar_cd as importer_ui
from cogs.tts.interface import editar_cd as editor_ui
from osaka_cd import discord_import as imports
from test_cd_editor_ui import _context, _interaction, _open_modal
from test_cd_discord_import_service import midi


def _text(view):
    return json.dumps(view.to_components(), ensure_ascii=False)


def _result():
    return imports.ImportResult(({'id': 'song', 'name': 'Song', 'artist': 'Composer',
                                  'notes': 2, 'duration_ms': 1000},),
                                applied=True, state='activated')


@pytest.mark.asyncio
async def test_standalone_command_reopens_idle_editor_and_invalidates_old_modal(monkeypatch):
    cog, message, response, tasks, service = _context(monkeypatch)
    assert await importer_ui.handle_import_cd(cog, message)
    previous, modal = await _open_modal(cog)
    epoch = previous.modal_epoch
    assert previous.waiting
    message.id += 1
    assert await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert current is not previous and current.waiting
    assert previous.finished and previous.modal_epoch > epoch
    assert not tasks
    assert 'Importação em andamento' not in _text(message.channel.send.await_args.kwargs['view'])
    interaction = _interaction()
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()
    service.edit_cd.assert_not_awaited()
    assert getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE) is current
    current.release()


@pytest.mark.asyncio
async def test_unauthorized_new_command_cannot_retire_owner_editor(monkeypatch):
    cog, message, _, _, _ = _context(monkeypatch)
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    cog.bot.is_owner.return_value = False
    await importer_ui.handle_import_cd(cog, message)
    assert getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE) is current
    assert not current.finished
    assert 'Acesso restrito' in _text(message.channel.send.await_args.kwargs['view'])
    current.release()


@pytest.mark.asyncio
async def test_midi_command_can_start_after_an_idle_editor_without_false_busy(monkeypatch):
    cog, message, response, tasks, service = _context(monkeypatch)
    await importer_ui.handle_import_cd(cog, message)
    previous = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    attachment = SimpleNamespace(filename='Composer - Song.mid', size=len(midi()),
                                 read=AsyncMock(return_value=midi()))
    message.attachments = [attachment]
    imported = AsyncMock(return_value=_result())
    monkeypatch.setattr(imports, 'import_cd', imported)
    assert await importer_ui.handle_import_cd(cog, message)
    assert previous.finished
    await asyncio.gather(*tasks)
    imported.assert_awaited_once()
    attachment.read.assert_awaited_once()
    service.edit_cd.assert_not_awaited()
    assert not hasattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert 'CD importado' in _text(response.edit.await_args.kwargs['view'])


@pytest.mark.asyncio
async def test_real_save_blocks_commands_then_finished_editor_can_reopen(monkeypatch):
    cog, message, _, tasks, service = _context(monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def save(*args, **kwargs):
        entered.set()
        await finish.wait()
        return SimpleNamespace(state='activated')
    service.edit_cd.side_effect = save
    await importer_ui.handle_import_cd(cog, message)
    previous, modal = await _open_modal(cog)
    await modal.on_submit(_interaction())
    await asyncio.wait_for(entered.wait(), 2)
    before = service.list_editable_cds.call_count
    await importer_ui.handle_import_cd(cog, message)
    assert getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE) is previous
    assert service.list_editable_cds.call_count == before
    assert 'Salvando CD' in _text(message.channel.send.await_args.kwargs['view'])
    finish.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert previous.waiting and previous.save_task is None
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert current is not previous and previous.finished
    current.release()


@pytest.mark.asyncio
async def test_cancelled_save_retains_lease_until_physical_commit_drain(monkeypatch):
    cog, message, _, tasks, service = _context(monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    def physical_commit():
        entered.set()
        assert finish.wait(5)
        return SimpleNamespace(state='activated')
    async def save(*args, **kwargs):
        return await imports._off_loop(physical_commit)
    service.edit_cd.side_effect = save
    await importer_ui.handle_import_cd(cog, message)
    previous, modal = await _open_modal(cog)
    await modal.on_submit(_interaction())
    assert await asyncio.to_thread(entered.wait, 2)
    task = tasks[0]
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    await importer_ui.handle_import_cd(cog, message)
    assert getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE) is previous
    assert 'Salvando CD' in _text(message.channel.send.await_args.kwargs['view'])
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert previous.finished and not hasattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert current.waiting
    current.release()


@pytest.mark.asyncio
async def test_finished_stale_lease_is_cleared_but_running_import_is_not(monkeypatch):
    cog, message, _, _, _ = _context(monkeypatch)
    setattr(cog.bot, importer_ui._LEASE_ATTRIBUTE, SimpleNamespace(finished=True))
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert isinstance(current, editor_ui._EditorSession)
    current.release()
    finish = asyncio.Event()
    worker = asyncio.create_task(finish.wait())
    lease = SimpleNamespace(finished=True, work_task=worker)
    setattr(cog.bot, importer_ui._LEASE_ATTRIBUTE, lease)
    await importer_ui.handle_import_cd(cog, message)
    assert getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE) is lease
    assert 'Importação em andamento' in _text(message.channel.send.await_args.kwargs['view'])
    finish.set()
    await worker
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert isinstance(current, editor_ui._EditorSession)
    current.release()


@pytest.mark.asyncio
async def test_deleted_old_editor_message_does_not_leave_false_busy(monkeypatch):
    cog, message, response, _, _ = _context(monkeypatch)
    await importer_ui.handle_import_cd(cog, message)
    previous = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    response.edit.side_effect = OSError('deleted old message')
    newer_response = SimpleNamespace(id=11, edit=AsyncMock())
    message.channel.send.return_value = newer_response
    old_edits = response.edit.await_count
    await importer_ui.handle_import_cd(cog, message)
    current = getattr(cog.bot, importer_ui._LEASE_ATTRIBUTE)
    assert current is not previous and previous.finished
    assert current.response is newer_response and current.waiting
    assert response.edit.await_count == old_edits
    current.release()
