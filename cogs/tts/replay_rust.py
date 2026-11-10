"""Asyncio ownership bridge for native multi-reader replay storage.

Control waits stay in asyncio; Rust owns all bytes, shared RAM reservations,
spool descriptors and atomic cache publication. A selected buffer never changes
backend while a stream is running.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any


def enabled() -> bool:
    return (os.environ.get("TTS_BACKEND", "").strip().lower() == "rust"
            or os.environ.get("TTS_REPLAY_RUST", "0").strip() == "1")


def native_module():
    from .fluxo_rust import _carregar_nucleo
    module = _carregar_nucleo()
    if (getattr(module, "REPLAY_PROTOCOL_VERSION", None) != 1
            or not hasattr(module, "ReplayMemoryBudget")
            or not hasattr(module, "NativeReplayBuffer")):
        raise RuntimeError("extensão Rust incompatível com o protocolo de replay 1")
    return module


def create_budget(limit: int):
    return native_module().ReplayMemoryBudget(limit)


class NativeReplayBuffer:
    backend = "rust"

    def __init__(self, directory: str, budget: Any, *, memory_limit: int, max_bytes: int):
        if getattr(budget, "_native", None) is None:
            raise RuntimeError("replay Rust exige orçamento Rust criado antes do stream")
        self.directory, self.budget = directory, budget
        self.memory_limit, self.max_bytes = memory_limit, max_bytes
        self._native = native_module().NativeReplayBuffer(
            os.fspath(directory), budget._native, memory_limit, max_bytes)
        self.condition = asyncio.Condition()
        self.size = 0
        self.ended = False
        self.error: BaseException | None = None
        self.closed = False
        self._pending: set[asyncio.Task] = set()
        self._native_closed = False

    async def _notify(self) -> None:
        async with self.condition:
            self.condition.notify_all()

    def _close_when_idle(self) -> None:
        if self.closed and not self._pending and not self._native_closed:
            self._native.close()
            self._native_closed = True

    async def _io(self, function, *args):
        # asyncio cancellation cannot revoke a descriptor from a running
        # thread. Hold this buffer until the physical operation has completed.
        from .runtime import await_physical_completion
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._pending.add(task)
        try:
            return await await_physical_completion(task)
        finally:
            self._pending.discard(task)
            self._close_when_idle()

    async def append(self, data: bytes) -> None:
        if not data or self.closed or self.ended:
            return
        data = bytes(data)
        immediate = getattr(self._native, "try_append_in_memory", None)
        if immediate is not None and immediate(data):
            self.size += len(data)
            await self._notify()
            return
        try:
            await self._io(self._native.append, data)
        finally:
            # An append can physically complete after coroutine cancellation.
            # Publish its committed size before waking readers in either case.
            self.size = self._native.snapshot()["size"]
            await self._notify()

    async def finish(self, error: BaseException | None = None) -> None:
        try:
            await self._io(self._native.finish, None if error is None else str(error))
        finally:
            self.error, self.ended = error, True
            await self._notify()

    async def wait_for_bytes(self, minimum: int = 1) -> None:
        async with self.condition:
            await self.condition.wait_for(lambda: self.size >= minimum or self.ended or self.closed)
        if self.closed:
            raise RuntimeError("buffer TTS fechado")
        if self.size == 0 or (self.ended and self.error is not None):
            if self.error is not None:
                raise self.error
            raise RuntimeError("engine não enviou áudio")

    async def chunks(self, *, chunk_size: int = 16384):
        if chunk_size <= 0:
            raise ValueError("chunk_size deve ser positivo")
        offset = 0
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: offset < self.size or self.ended or self.closed)
                available = min(chunk_size, self.size - offset)
                ended, error = self.ended, self.error
            if self.closed:
                raise RuntimeError("buffer TTS fechado")
            if available:
                immediate = getattr(self._native, "try_read_in_memory", None)
                data = immediate(offset, available) if immediate is not None else None
                if data is None:
                    data = await self._io(self._native.read_at, offset, available)
                if not data:
                    raise RuntimeError("spool TTS incompleto")
                offset += len(data)
                yield data
            elif ended:
                if error is not None:
                    raise error
                return

    async def copy_to(self, path: str) -> None:
        if not self.ended or self.error is not None:
            raise RuntimeError("áudio incompleto não pode entrar no cache")
        await self._io(self._native.copy_to, os.fspath(path))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._close_when_idle()
        try:
            asyncio.get_running_loop().create_task(self._notify())
        except RuntimeError:
            pass  # No waiters can be running after their event loop has closed.
