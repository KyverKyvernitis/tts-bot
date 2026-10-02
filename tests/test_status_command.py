from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utility.commands.status import StatusCommandMixin
from utility.status.data import StatusSnapshot, StorageSnapshot
from utility.status.views import StatusView
from utility.technical_commands import TECHNICAL_COMMAND_GUILD_ID


class Cog(StatusCommandMixin):
    def __init__(self, collector):
        self.bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))
        self._status_collector = collector


def context():
    message = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        author=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=TECHNICAL_COMMAND_GUILD_ID),
        prefix='_', clean_prefix='_', send=AsyncMock(return_value=message),
    ), message


def snapshot():
    return StatusSnapshot(1700000000, (), True, True, True, {}, (),
                          StorageSnapshot(1700000000, 0, 0, 0, 0), None)


@pytest.mark.asyncio
async def test_status_guard_and_invalid_arguments_do_not_collect():
    collector = SimpleNamespace(collect=AsyncMock(return_value=snapshot()))
    cog = Cog(collector)
    ctx, _ = context()
    cog.bot.is_owner.return_value = False
    await StatusCommandMixin.status.callback(cog, ctx)
    assert 'exclusivo' in ctx.send.call_args.args[0]
    collector.collect.assert_not_awaited()

    cog.bot.is_owner.return_value = True
    await StatusCommandMixin.status.callback(cog, ctx, section='invalid')
    assert 'status servidores' in ctx.send.call_args.args[0]
    assert 'status tts' in ctx.send.call_args.args[0]
    collector.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_opens_tts_and_unload_disables_its_tracked_controls():
    collector = SimpleNamespace(collect=AsyncMock(return_value=snapshot()))
    cog = Cog(collector)
    ctx, message = context()
    await StatusCommandMixin.status.callback(cog, ctx, section='tts')
    view, = cog._status_views
    assert view.tab == 'tts'
    assert view.owner_id == ctx.author.id
    assert view.message is message
    assert message.edit.call_args.kwargs['view'] is view
    cog._stop_status_views()
    assert view.closed
    assert not cog._status_views
    await view.close()
    await asyncio.sleep(0)
    assert all(item.disabled for item in view.walk_children() if hasattr(item, 'disabled'))


@pytest.mark.asyncio
async def test_unload_during_collection_never_creates_an_active_view():
    started, release = asyncio.Event(), asyncio.Event()

    async def collect():
        started.set()
        await release.wait()
        return snapshot()

    cog = Cog(SimpleNamespace(collect=collect))
    ctx, message = context()
    request = asyncio.create_task(StatusCommandMixin.status.callback(cog, ctx))
    await started.wait()
    cog._stop_status_views()
    release.set()
    await request
    assert not getattr(cog, '_status_views', ())
    assert not isinstance(message.edit.call_args.kwargs['view'], StatusView)


@pytest.mark.asyncio
async def test_unload_during_first_edit_closes_the_view_already_tracked():
    cog = Cog(SimpleNamespace(collect=AsyncMock(return_value=snapshot())))
    ctx, message = context()
    observed = []

    async def edit(**kwargs):
        view = kwargs['view']
        if isinstance(view, StatusView) and not view.closed:
            observed.append(view)
            assert view in cog._status_views
            cog._stop_status_views()

    message.edit.side_effect = edit
    await StatusCommandMixin.status.callback(cog, ctx)
    await asyncio.sleep(0)
    assert len(observed) == 1
    assert observed[0].closed
    assert not cog._status_views
