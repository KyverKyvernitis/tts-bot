"""Async transport for Rust-owned native engines; no TTS business logic.

Cancellation waits for physical native I/O completion before returning, so the
caller's source/cache lease cannot be released while a motor still uses it.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any


def _module():
    from .fluxo_rust import _carregar_nucleo
    module = _carregar_nucleo()
    if getattr(module, "NATIVE_ENGINES_PROTOCOL_VERSION", None) != 1:
        raise RuntimeError("native engines Rust protocol 1 is required")
    return module


async def _physical_call(operation, cancel, *args):
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel()
        # Repeated caller cancellation never releases ownership prematurely.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel()
            except Exception:
                break
        with contextlib.suppress(BaseException):
            task.result()
        raise


async def worldline(command: list[str], *, timeout_ms: int = 20_000,
                    environment: dict[str, str] | None = None) -> str:
    module = _module()
    cancellation = module.NativeEngineCancellation()
    return await _physical_call(module.native_worldline, cancellation.cancel,
                                command, timeout_ms, cancellation, environment)


async def resampler(command: list[str], args: list[str], *, timeout_ms: int = 20_000,
                    environment: dict[str, str] | None = None) -> None:
    module = _module()
    cancellation = module.NativeEngineCancellation()
    await _physical_call(module.native_resampler, cancellation.cancel,
                         command, args, timeout_ms, cancellation, environment)


class EsperResident:
    """An async adapter to one serialized Rust-owned ESPER resident process."""
    def __init__(self, session: Any):
        self._session = session

    @classmethod
    async def create(cls, command: list[str], *, timeout_ms: int = 20_000,
                     environment: dict[str, str] | None = None):
        module = _module()
        task = asyncio.create_task(asyncio.to_thread(
            module.NativeEsperResident, command, environment, timeout_ms))
        try:
            return cls(await asyncio.shield(task))
        except asyncio.CancelledError:
            # The constructor is bounded by its native handshake deadline.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.exception() is None:
                task.result().cancel()
                # close releases the GIL while the child is physically reaped.
                closing = asyncio.create_task(asyncio.to_thread(task.result().close))
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        continue
                closing.result()
            raise

    async def render(self, args: list[str], *, timeout_ms: int = 20_000,
                     headroom: bool = True) -> str:
        return await _physical_call(self._session.render, self._session.cancel,
                                    args, timeout_ms, headroom)

    async def close(self) -> None:
        self._session.cancel()
        await _physical_call(self._session.close, self._session.cancel)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
