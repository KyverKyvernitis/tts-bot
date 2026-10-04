"""Um heartbeat por canal cobre respostas e ações sem afetar o trabalho real."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.context_managers import Typing
import pytest

from cogs.chatbot.typing import ProcessingIndicator


async def eventually(predicate, *, timeout=1.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), timeout)


def channel(identifier=20, *, sender=None):
    return SimpleNamespace(id=identifier, typing=sender or AsyncMock())


def active_typing_tasks():
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("chatbot-typing:") and not task.done()]


@pytest.mark.asyncio
async def test_indicator_starts_immediately_renews_and_stops_when_work_finishes():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = channel()
    async with indicator.process(target):
        await eventually(lambda: target.typing.await_count >= 2)
        assert len(active_typing_tasks()) == 1
    count = target.typing.await_count
    await asyncio.sleep(.02)
    assert target.typing.await_count == count
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_nested_work_shares_one_worker_and_last_context_owns_cleanup():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = channel()
    async with indicator.process(target):
        await eventually(lambda: target.typing.await_count > 0)
        state = indicator._channels[20]
        original_task = state.task
        async with indicator.process(target):
            assert state.references == 2 and state.task is original_task
            assert len(active_typing_tasks()) == 1
        assert state.references == 1 and not original_task.done()
        previous = target.typing.await_count
        await eventually(lambda: target.typing.await_count > previous)
    assert original_task.done() and indicator._channels == {}


@pytest.mark.asyncio
async def test_overlapping_reply_and_automatic_action_share_same_channel_worker():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = channel()
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    first_finish, second_finish = asyncio.Event(), asyncio.Event()

    async def process(entered, finish):
        async with indicator.process(target):
            entered.set()
            await finish.wait()

    first = asyncio.create_task(process(first_entered, first_finish))
    second = asyncio.create_task(process(second_entered, second_finish))
    await asyncio.wait_for(asyncio.gather(first_entered.wait(), second_entered.wait()), 1)
    await eventually(lambda: target.typing.await_count > 0)
    assert indicator._channels[20].references == 2 and len(active_typing_tasks()) == 1
    first_finish.set()
    await first
    state = indicator._channels[20]
    assert state.references == 1 and not state.task.done()
    second_finish.set()
    await second
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_channels_have_independent_workers_and_capacity_does_not_block_work():
    indicator = ProcessingIndicator(interval_seconds=.01, max_channels=2)
    first, second, extra = channel(20), channel(21), channel(22)
    async with indicator.process(first), indicator.process(second), indicator.process(extra):
        await eventually(lambda: first.typing.await_count and second.typing.await_count)
        assert len(indicator._channels) == 2 and len(active_typing_tasks()) == 2
        extra.typing.assert_not_called()
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_same_channel_id_with_new_wrapper_still_has_one_worker():
    indicator = ProcessingIndicator(interval_seconds=.01)
    first, second = channel(20), channel(20)
    async with indicator.process(first):
        await eventually(lambda: first.typing.await_count > 0)
        worker = indicator._channels[20].task
        async with indicator.process(second):
            await eventually(lambda: second.typing.await_count > 0)
            assert indicator._channels[20].task is worker and len(active_typing_tasks()) == 1
    assert active_typing_tasks() == []


@pytest.mark.asyncio
async def test_real_discord_typing_object_is_awaited_once_without_sdk_background_worker():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = SimpleNamespace(id=20)
    target._state = SimpleNamespace(loop=asyncio.get_running_loop(), http=SimpleNamespace(send_typing=AsyncMock()))
    target._get_channel = AsyncMock(return_value=target)
    created = []

    def typing():
        instance = Typing(target)
        created.append(instance)
        return instance

    target.typing = typing
    async with indicator.process(target):
        await eventually(lambda: target._state.http.send_typing.await_count >= 2)
        assert len(active_typing_tasks()) == 1
        assert all(not hasattr(instance, "task") for instance in created)
    assert active_typing_tasks() == []


@pytest.mark.asyncio
async def test_trigger_typing_compatibility_and_unsupported_channel_do_not_abort_work():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = SimpleNamespace(id=20, trigger_typing=AsyncMock())
    async with indicator.process(target):
        await eventually(lambda: target.trigger_typing.await_count > 0)
    unavailable = SimpleNamespace(id=20)
    async with indicator.process(unavailable):
        await eventually(lambda: indicator._channels[20].task.done())
        replacement = channel(20)
        async with indicator.process(replacement):
            await eventually(lambda: replacement.typing.await_count > 0)
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, SimpleNamespace(), channel(None), channel(True), channel(0), channel(-1), channel("bad")])
async def test_invalid_or_unavailable_channel_is_a_noop(target):
    indicator = ProcessingIndicator(interval_seconds=.01)
    async with indicator.process(target):
        assert indicator._channels == {}
    await indicator.close()
    assert indicator.closed


@pytest.mark.asyncio
async def test_http_failure_keeps_real_work_running_and_logs_only_error_type(caplog):
    secret = "PRIVATE_SPEECH_OR_TOKEN_DO_NOT_LOG"
    target = channel(sender=AsyncMock(side_effect=RuntimeError(secret)))
    indicator = ProcessingIndicator(interval_seconds=.01)
    async with indicator.process(target):
        await eventually(lambda: target.typing.await_count >= 2)
    assert "RuntimeError" in caplog.text and "channel=20" in caplog.text
    assert secret not in caplog.text
    assert len(caplog.records) == 1 and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_http_timeout_is_bounded_and_finally_awaits_cancelled_request():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    target = channel(sender=stalled)
    indicator = ProcessingIndicator(interval_seconds=.01, timeout_seconds=.01)
    async with indicator.process(target):
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_work_exception_propagates_after_indicator_cleanup():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = channel()
    with pytest.raises(ValueError, match="actual failure"):
        async with indicator.process(target):
            await eventually(lambda: target.typing.await_count > 0)
            raise ValueError("actual failure")
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_cancellation_of_work_cleans_indicator_before_propagating():
    indicator = ProcessingIndicator(interval_seconds=.01)
    target = channel()
    entered = asyncio.Event()

    async def work():
        async with indicator.process(target):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 1)
    await eventually(lambda: target.typing.await_count > 0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_unload_cancels_and_awaits_every_worker_and_later_work_is_a_noop():
    indicator = ProcessingIndicator(interval_seconds=.01)
    first, second = channel(20), channel(21)
    async with indicator.process(first), indicator.process(second):
        await eventually(lambda: first.typing.await_count and second.typing.await_count)
        workers = [state.task for state in indicator._channels.values()]
        await indicator.close()
        assert indicator.closed and indicator._channels == {}
        assert all(worker.done() for worker in workers)
        async with indicator.process(channel(22)):
            assert indicator._channels == {}
    await indicator.close()
    assert active_typing_tasks() == []


@pytest.mark.asyncio
async def test_new_work_waits_for_previous_channel_worker_cancellation_then_uses_fresh_worker():
    cancelled, release = asyncio.Event(), asyncio.Event()
    first_entered, first_finish, second_entered, second_finish = [asyncio.Event() for _ in range(4)]

    async def sender():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    indicator = ProcessingIndicator(interval_seconds=.01, timeout_seconds=5)
    first_channel, second_channel = channel(sender=sender), channel()

    async def work(target, entered, finish):
        async with indicator.process(target):
            entered.set()
            await finish.wait()

    first = asyncio.create_task(work(first_channel, first_entered, first_finish))
    await asyncio.wait_for(first_entered.wait(), 1)
    await eventually(lambda: indicator._channels[20].task is not None)
    await asyncio.sleep(.001)
    first_finish.set()
    await asyncio.wait_for(cancelled.wait(), 1)
    second = asyncio.create_task(work(second_channel, second_entered, second_finish))
    await asyncio.sleep(.005)
    assert not second_entered.is_set()
    release.set()
    await first
    await asyncio.wait_for(second_entered.wait(), 1)
    await eventually(lambda: second_channel.typing.await_count > 0)
    assert len(active_typing_tasks()) == 1
    second_finish.set()
    await second
    assert indicator._channels == {} and active_typing_tasks() == []


@pytest.mark.asyncio
async def test_unload_and_last_exit_share_cancellation_and_wait_for_sdk_cleanup():
    entered, cancelled, release = [asyncio.Event() for _ in range(3)]
    work_entered, finish = asyncio.Event(), asyncio.Event()
    cancellations = []

    async def sender():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellations.append("first")
            cancelled.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append("second")
                raise
            raise

    indicator = ProcessingIndicator(interval_seconds=.01, timeout_seconds=5)
    target = channel(sender=sender)

    async def work():
        async with indicator.process(target):
            work_entered.set()
            await finish.wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(work_entered.wait(), 1)
    await asyncio.wait_for(entered.wait(), 1)
    finish.set()
    await asyncio.wait_for(cancelled.wait(), 1)
    shutdown = asyncio.create_task(indicator.close())
    await asyncio.sleep(.005)
    assert not shutdown.done() and not task.done() and cancellations == ["first"]
    release.set()
    await asyncio.wait_for(asyncio.gather(task, shutdown), 1)
    assert cancellations == ["first"] and indicator._channels == {} and active_typing_tasks() == []
