from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_transport():
    root = Path(__file__).resolve().parents[1]
    path = root / "deploy/termux/phone-worker/tts_transport.py"
    spec = importlib.util.spec_from_file_location("phone_worker_tts_transport_py310_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_phone_worker_edge_transport_does_not_require_asyncio_timeout(monkeypatch):
    transport = _load_transport()
    received = []

    class Communicate:
        def __init__(self, **kwargs):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"edge-ok"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    monkeypatch.setattr(
        asyncio,
        "timeout",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("asyncio.timeout não pode ser usado")),
    )

    stream = object.__new__(transport.AudioStream)
    stream.text = "teste"
    stream.voice = "pt-BR-FranciscaNeural"
    stream.rate = "+0%"
    stream.pitch = "+0Hz"
    stream.deadline = time.monotonic() + 1.0
    stream.error = None
    stream._task = None
    stream._check = lambda: None
    stream._put = lambda data: received.append(data)
    stream._finish = lambda: None

    await stream._edge()

    assert stream.error is None
    assert received == [b"edge-ok"]
