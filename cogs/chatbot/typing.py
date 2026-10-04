"""Um indicador Discord de digitação por canal enquanto há trabalho ativo."""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _ChannelProcessing:
    channel: object
    references: int = 0
    task: asyncio.Task | None = None
    closing: bool = False
    cancel_requested: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class ProcessingIndicator:
    """Refcount de turnos e ações; falhas do indicador não abortam a resposta."""
    def __init__(self, *, interval_seconds: float = 5.0, timeout_seconds: float = 3.0,
                 max_channels: int = 128):
        self._interval = max(.001, float(interval_seconds))
        self._timeout = max(.001, float(timeout_seconds))
        self._max_channels = max(1, int(max_channels))
        self._channels: dict[int, _ChannelProcessing] = {}
        self.closed = False

    @staticmethod
    def _cancel(state: _ChannelProcessing) -> None:
        # close() e o último __aexit__ podem acontecer ao mesmo tempo. Cancelar
        # uma única vez permite que o SDK termine a requisição pendente.
        if state.task is not None and not state.task.done() and not state.cancel_requested:
            state.cancel_requested = True
            state.task.cancel()

    @staticmethod
    def _channel_id(channel) -> int | None:
        value = getattr(channel, "id", None)
        if isinstance(value, bool):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if value > 0 else None

    async def _send(self, channel) -> bool:
        # discord.py 2.x permite await channel.typing() para um único evento.
        # Assim o heartbeat pertence a este manager, sem uma segunda task do SDK.
        sender = getattr(channel, "trigger_typing", None)
        if not callable(sender):
            sender = getattr(channel, "typing", None)
        if not callable(sender):
            return False
        result = sender()
        if not inspect.isawaitable(result):
            return False
        await result
        return True

    async def _heartbeat(self, channel_id: int, state: _ChannelProcessing) -> None:
        warned = False
        while not self.closed and state.references > 0 and not state.closing:
            try:
                supported = await asyncio.wait_for(self._send(state.channel), timeout=self._timeout)
                if not supported:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not warned:
                    log.warning("chatbot: indicador indisponível | channel=%s erro_tipo=%s", channel_id, type(exc).__name__)
                    warned = True
            await asyncio.sleep(self._interval)

    @asynccontextmanager
    async def process(self, channel):
        """No-op para canal indisponível; contextos sobrepostos usam uma task."""
        channel_id = self._channel_id(channel)
        state = None
        while channel_id is not None and not self.closed:
            current = self._channels.get(channel_id)
            if current is not None and current.closing:
                await current.closed.wait()
                continue
            if current is None:
                if len(self._channels) >= self._max_channels:
                    break
                current = _ChannelProcessing(channel=channel)
                self._channels[channel_id] = current
            current.channel = channel
            current.references += 1
            if current.task is None or current.task.done():
                current.cancel_requested = False
                current.task = asyncio.create_task(self._heartbeat(channel_id, current), name=f"chatbot-typing:{channel_id}")
            state = current
            break
        try:
            yield
        finally:
            if state is not None:
                state.references = max(0, state.references - 1)
                if state.references == 0 and not state.closing:
                    state.closing = True
                    self._cancel(state)
                    try:
                        if state.task is not None:
                            await asyncio.gather(state.task, return_exceptions=True)
                    finally:
                        if self._channels.get(channel_id) is state:
                            self._channels.pop(channel_id, None)
                        state.closed.set()

    async def close(self) -> None:
        """Cancela e aguarda todas as tasks; novos contextos ficam sem indicador."""
        self.closed = True
        states = list(self._channels.values())
        tasks = []
        for state in states:
            state.closing = True
            if state.task is not None:
                self._cancel(state)
                tasks.append(state.task)
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._channels.clear()
            for state in states:
                state.closed.set()
