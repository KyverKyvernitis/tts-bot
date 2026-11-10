"""Native ownership must survive repeated asyncio cancellation."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import threading
import time

import pytest


def _adapter():
    path = Path(__file__).resolve().parents[1] / "cogs/tts/engines_rust.py"
    spec = importlib.util.spec_from_file_location("native_engines_adapter_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_motor_and_preserves_event_loop():
    adapter = _adapter()
    entered, cancelled, physically_done = (threading.Event() for _ in range(3))

    def motor():
        entered.set()
        assert cancelled.wait(2)
        time.sleep(.05)
        physically_done.set()
        raise RuntimeError("native cancelled after physical shutdown")

    operation = asyncio.create_task(adapter._physical_call(motor, cancelled.set))
    assert await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(.005)
    operation.cancel()
    assert not physically_done.is_set()
    # The async loop can continue while Rust/the bounded worker stops the motor.
    await asyncio.sleep(.005)
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert physically_done.is_set()


@pytest.mark.asyncio
async def test_motor_failure_is_visible_and_does_not_invoke_fallback():
    adapter = _adapter()
    cancelled = threading.Event()

    def motor():
        raise OSError("native library missing")

    with pytest.raises(OSError, match="native library missing"):
        await adapter._physical_call(motor, cancelled.set)
    assert not cancelled.is_set()
