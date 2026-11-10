"""Real Tokio/asyncio bridge contracts against a local Edge WebSocket fixture.

The fixture never contacts a provider or Discord. Set TTS_NATIVE_REQUIRED=1 in
the integration gate to turn an absent extension into an error, not a skip.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os

from aiohttp import web
import pytest


@pytest.fixture
def native(monkeypatch):
    from cogs.tts.fluxo_rust import _carregar_nucleo
    try:
        module = _carregar_nucleo()
        if (getattr(module, "NATIVE_PROVIDERS_PROTOCOL_VERSION", None) != 1
                or not callable(getattr(module, "provider_start", None))):
            raise RuntimeError("native provider protocol 1 is required")
    except (ImportError, OSError, RuntimeError) as error:
        if os.environ.get("TTS_NATIVE_REQUIRED") == "1":
            raise
        pytest.skip(str(error))
    # The managed environment's HTTPS proxy remains enabled for remote URLs.
    # Only the local test server bypasses it.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    return module


def audio_frame(data: bytes) -> bytes:
    header = b"Path:audio\r\nContent-Type:audio/mpeg\r\n"
    return len(header).to_bytes(2, "big") + header + data


@asynccontextmanager
async def edge_server(callback):
    sockets = set()

    async def handle(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.add(socket)
        try:
            for _ in range(2):
                incoming = await socket.receive(timeout=2)
                assert incoming.type == web.WSMsgType.TEXT
            await socket.send_str("Path:turn.start\r\n\r\n")
            await socket.send_str("Path:response\r\n\r\n")
            await callback(socket)
            return socket
        finally:
            sockets.discard(socket)
            await socket.close()

    application = web.Application()
    application.router.add_get("/synthesis", handle)
    runner = web.AppRunner(application, shutdown_timeout=.2)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}/synthesis"
    finally:
        await asyncio.gather(*(socket.close() for socket in tuple(sockets)), return_exceptions=True)
        await runner.cleanup()


async def start(native, endpoint):
    return await native.provider_start(json.dumps({
        "engine": "edge", "text": "Olá <mundo>", "voice": "pt-BR-FranciscaNeural",
        "endpoint": endpoint, "timeout_seconds": 3.0, "max_audio_bytes": 65536,
    }))


@pytest.mark.asyncio
async def test_first_audio_arrives_before_provider_completes_and_loop_keeps_running(native):
    release = asyncio.Event()

    async def server(socket):
        await socket.send_bytes(audio_frame(b"first"))
        await release.wait()
        await socket.send_bytes(audio_frame(b"second"))
        await socket.send_str("Path:turn.end\r\n\r\n")

    async with edge_server(server) as endpoint:
        stream = await start(native, endpoint)
        try:
            assert await asyncio.wait_for(stream.next_chunk(), 2) == b"first"
            assert not release.is_set()
            next_chunk = asyncio.ensure_future(stream.next_chunk())
            await asyncio.sleep(.01)
            assert not next_chunk.done()
            release.set()
            assert await asyncio.wait_for(next_chunk, 2) == b"second"
            assert await asyncio.wait_for(stream.next_chunk(), 2) is None
            metadata = json.loads(await stream.metadata())
            assert metadata["engine"] == "edge"
            assert metadata["audio_format"] == "mp3"
        finally:
            release.set()
            stream.cancel()


@pytest.mark.asyncio
async def test_partial_provider_close_is_error_instead_of_successful_eof(native):
    async def server(socket):
        await socket.send_bytes(audio_frame(b"partial"))
        await socket.close()

    async with edge_server(server) as endpoint:
        stream = await start(native, endpoint)
        try:
            assert await asyncio.wait_for(stream.next_chunk(), 2) == b"partial"
            with pytest.raises(RuntimeError) as raised:
                await asyncio.wait_for(stream.next_chunk(), 2)
            assert raised.value.kind == "incomplete"
        finally:
            stream.cancel()


@pytest.mark.asyncio
async def test_provider_pumps_directly_into_native_replay_with_independent_readers(native, tmp_path):
    async def server(socket):
        await socket.send_bytes(audio_frame(b"one"))
        await socket.send_bytes(audio_frame(b"two"))
        await socket.send_str("Path:turn.end\r\n\r\n")

    budget = native.ReplayMemoryBudget(4)
    replay = native.NativeReplayBuffer(str(tmp_path), budget, 4, 100)
    async with edge_server(server) as endpoint:
        stream = await start(native, endpoint)
        try:
            progress = []
            while (record := await asyncio.wait_for(stream.pump_chunk(replay), 2)) is not None:
                progress.append(record)
            replay.finish(None)
            assert progress[-1][0] == 6
            assert sum(record[1] for record in progress) == 6
            assert all(record[2] >= 0 for record in progress)
            assert replay.snapshot()["spilled"] is True
            assert replay.read_at(0, 6) == b"onetwo"
            assert replay.read_at(3, 3) == b"two"
            assert replay.read_at(0, 3) == b"one"
        finally:
            stream.cancel()
            replay.close()
    assert budget.used == 0


@pytest.mark.asyncio
async def test_cancel_unblocks_waiting_native_read_with_cancellation_kind(native):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def server(socket):
        entered.set()
        await release.wait()

    async with edge_server(server) as endpoint:
        stream = await start(native, endpoint)
        try:
            waiting = asyncio.ensure_future(stream.next_chunk())
            await asyncio.wait_for(entered.wait(), 2)
            stream.cancel()
            with pytest.raises(RuntimeError) as raised:
                await asyncio.wait_for(waiting, 1)
            assert raised.value.kind == "cancelled"
        finally:
            release.set()
            stream.cancel()
