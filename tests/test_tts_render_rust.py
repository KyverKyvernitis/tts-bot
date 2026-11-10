"""Renderer cancellation remains pending until native ownership ends."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


def adapter():
    path = Path(__file__).resolve().parents[1] / "cogs/tts/render_rust.py"
    spec = importlib.util.spec_from_file_location("native_renderer_adapter_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_renderer_repeated_cancellation_waits_for_physical_shutdown(monkeypatch):
    renderer = adapter()
    entered, cancelled, release, stopped = (threading.Event() for _ in range(4))

    class Cancellation:
        def cancel(self):
            cancelled.set()

    def native_render(request, token):
        entered.set()
        assert release.wait(3)
        stopped.set()
        raise RuntimeError("native process reaped")

    monkeypatch.setattr(renderer, "native_module", lambda: SimpleNamespace(
        NativeRenderCancellation=Cancellation, ttv_render_json=native_render))
    operation = asyncio.create_task(renderer.render({"text": "olá"}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation.cancel()
        assert await asyncio.to_thread(cancelled.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert not stopped.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_renderer_native_failure_is_propagated(monkeypatch):
    renderer = adapter()

    def failed(request, token):
        raise RuntimeError("native analysis failed")

    monkeypatch.setattr(renderer, "native_module", lambda: SimpleNamespace(
        NativeRenderCancellation=lambda: object(), ttv_render_json=failed))
    with pytest.raises(RuntimeError, match="native analysis failed"):
        await renderer.render({"text": "olá"})
