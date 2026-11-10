"""Thin asyncio adapter for native provider operations; no provider imports."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os


def enabled() -> bool:
    return (os.environ.get("TTS_PROVIDERS_RUST", "0").strip() == "1"
            or os.environ.get("TTS_BACKEND", "").strip().lower() == "rust")


def native_module():
    from .fluxo_rust import _carregar_nucleo
    module = _carregar_nucleo()
    if getattr(module, "NATIVE_PROVIDERS_PROTOCOL_VERSION", None) != 1:
        raise RuntimeError("extensão Rust incompatível com provedores nativos 1")
    return module


async def start(request: dict):
    return await native_module().provider_start(json.dumps(request, ensure_ascii=False, allow_nan=False))


async def chunks(request: dict):
    stream = await start(request)
    try:
        while True:
            data = await stream.next_chunk()
            if data is None:
                return
            yield data
    finally:
        stream.cancel()


async def pump(stream, buffer):
    """Keep bytes in Rust and publish only committed progress to asyncio.

    The physical append finishes before cancellation can release the owning
    buffer, matching the disk-I/O ownership of the existing replay adapter.
    """
    from .runtime import await_physical_completion
    while True:
        task = asyncio.ensure_future(stream.pump_chunk(buffer._native))
        buffer._pending.add(task)
        try:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                stream.cancel()
                with contextlib.suppress(BaseException):
                    await await_physical_completion(task)
                raise
        finally:
            buffer._pending.discard(task)
            buffer.size = buffer._native.snapshot()["size"]
            await buffer._notify()
            buffer._close_when_idle()
        if result is None:
            return
        yield result


async def synthesize(request: dict) -> tuple[bytes, dict]:
    stream = await start(request)
    output = bytearray()
    try:
        while True:
            data = await stream.next_chunk()
            if data is None:
                return bytes(output), json.loads(await stream.metadata())
            output.extend(data)
    finally:
        stream.cancel()


async def save(request: dict, path: str) -> dict:
    return json.loads(await native_module().provider_save(
        json.dumps(request, ensure_ascii=False, allow_nan=False), os.fspath(path)))


async def voices():
    return json.loads(await native_module().provider_voices())


def languages() -> dict[str, str]:
    return json.loads(native_module().provider_languages_json())
