import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from utility.base_archive import BaseArchiveError, BaseArchiveResult
from utility.commands import base as module
from utility.technical_commands import TECHNICAL_COMMAND_GUILD_ID, can_use_technical_command


def context(*, limit=1_000_000, guild_id=TECHNICAL_COMMAND_GUILD_ID):
    return SimpleNamespace(
        author=SimpleNamespace(id=42),
        guild=SimpleNamespace(id=guild_id, filesize_limit=limit),
        send=AsyncMock(),
    )


def command():
    instance = module.BaseCommandMixin()
    instance.bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))
    return instance


async def invoke(instance, ctx):
    await module.BaseCommandMixin.base.callback(instance, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [None, 0, TECHNICAL_COMMAND_GUILD_ID + 1, "invalid"])
async def test_guard_rejects_other_contexts_before_owner_check(guild_id):
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))
    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    assert not await can_use_technical_command(bot, SimpleNamespace(id=42), guild)
    bot.is_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_checks_owner_and_fails_closed():
    author = SimpleNamespace(id=42)
    guild = SimpleNamespace(id=TECHNICAL_COMMAND_GUILD_ID)
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))
    assert await can_use_technical_command(bot, author, guild)
    bot.is_owner.assert_awaited_once_with(author)
    bot.is_owner = AsyncMock(return_value=False)
    assert not await can_use_technical_command(bot, author, guild)
    bot.is_owner = AsyncMock(side_effect=RuntimeError("unavailable"))
    assert not await can_use_technical_command(bot, author, guild)


@pytest.mark.asyncio
async def test_unauthorized_command_never_accesses_service(monkeypatch):
    instance = command()
    instance.bot.is_owner.return_value = False
    get_service = Mock()
    monkeypatch.setattr(module, "get_base_archive_service", get_service)
    ctx = context()
    await invoke(instance, ctx)
    get_service.assert_not_called()
    assert "exclusivo" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_fast_result_sends_one_direct_attachment_with_mentions_disabled(monkeypatch):
    result = BaseArchiveResult(b"test-zip", "repo-test.zip", 3)
    service = SimpleNamespace(get_archive=AsyncMock(return_value=result))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    ctx = context()
    sent = []

    async def send(text, **kwargs):
        attachment = kwargs["file"]
        sent.append((text, attachment.filename, attachment.fp.read(), kwargs["allowed_mentions"]))

    ctx.send.side_effect = send
    await invoke(command(), ctx)
    assert len(sent) == 1
    assert sent[0][1:3] == ("repo-test.zip", b"test-zip")
    assert "3 arquivos" in sent[0][0]
    assert sent[0][3].to_dict()["parse"] == []
    assert module.BaseCommandMixin.base.name == "base"
    assert module.BaseCommandMixin.base.hidden


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 3])
async def test_guild_upload_limit_is_checked_before_attachment_send(monkeypatch, limit):
    service = SimpleNamespace(get_archive=AsyncMock(return_value=BaseArchiveResult(b"123456", "repo-test.zip", 1)))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    ctx = context(limit=limit)
    await invoke(command(), ctx)
    ctx.send.assert_awaited_once()
    assert "limite de anexos" in ctx.send.call_args.args[0]
    assert "file" not in ctx.send.call_args.kwargs


@pytest.mark.asyncio
async def test_unavailable_upload_limit_does_not_guess_or_send_file(monkeypatch):
    service = SimpleNamespace(get_archive=AsyncMock(return_value=BaseArchiveResult(b"123456", "repo-test.zip", 1)))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    ctx = context()
    del ctx.guild.filesize_limit
    await invoke(command(), ctx)
    assert "indisponível" in ctx.send.call_args.args[0]
    assert "file" not in ctx.send.call_args.kwargs


@pytest.mark.asyncio
async def test_slow_result_updates_preparation_message_with_attachment(monkeypatch):
    result = BaseArchiveResult(b"test-zip", "repo-test.zip", 3)
    service = SimpleNamespace(get_archive=AsyncMock(return_value=result))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    monkeypatch.setattr(module.asyncio, "wait", AsyncMock(return_value=(set(), set())))
    progress = SimpleNamespace(edit=AsyncMock())
    sent = []

    async def edit(**kwargs):
        sent.append(kwargs["attachments"][0].fp.read())

    progress.edit.side_effect = edit
    ctx = context()
    ctx.send.return_value = progress
    await invoke(command(), ctx)
    ctx.send.assert_awaited_once()
    assert "Preparando" in ctx.send.call_args.args[0]
    assert sent == [b"test-zip"]
    assert "Base Git leve" in progress.edit.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_generation_error_updates_preparation_without_raw_detail(monkeypatch):
    service = SimpleNamespace(get_archive=AsyncMock(side_effect=BaseArchiveError("O Git está indisponível.")))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    monkeypatch.setattr(module.asyncio, "wait", AsyncMock(return_value=(set(), set())))
    progress = SimpleNamespace(edit=AsyncMock())
    ctx = context()
    ctx.send.return_value = progress
    await invoke(command(), ctx)
    assert "O Git está indisponível." in progress.edit.call_args.kwargs["content"]
    assert progress.edit.call_args.kwargs["attachments"] == []


@pytest.mark.asyncio
async def test_send_failure_is_visible_and_does_not_expose_exception(monkeypatch):
    service = SimpleNamespace(get_archive=AsyncMock(return_value=BaseArchiveResult(b"test-zip", "repo-test.zip", 3)))
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    ctx = context()
    ctx.send.side_effect = [RuntimeError("TOKEN=private-placeholder"), None]
    await invoke(command(), ctx)
    assert ctx.send.await_count == 2
    assert "Não consegui enviar" in ctx.send.call_args.args[0]
    assert "private-placeholder" not in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_cancelling_command_cancels_only_its_request(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def request():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service = SimpleNamespace(get_archive=request)
    monkeypatch.setattr(module, "get_base_archive_service", lambda: service)
    task = asyncio.create_task(invoke(command(), context()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=1)
