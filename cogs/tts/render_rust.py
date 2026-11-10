"""Discord/platform adapter for native phonetics, TTV planning and rendering.

All synthesis decisions, engine supervision and PCM composition run in Rust.
Blocking calls release the GIL and run outside the asyncio event-loop thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os


def enabled() -> bool:
    return (os.environ.get("TTS_RENDER_RUST", "0").strip() == "1"
            or os.environ.get("TTS_BACKEND", "").strip().lower() == "rust")


def native_module():
    from .fluxo_rust import _carregar_nucleo
    module = _carregar_nucleo()
    if getattr(module, "NATIVE_RENDER_PROTOCOL_VERSION", None) != 1:
        raise RuntimeError("extensão Rust incompatível com renderer TTV 1")
    return module


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def domain(operation: str, payload: dict):
    return json.loads(native_module().ttv_domain_json(operation, _json(payload)))


async def plan(request: dict):
    native = native_module()
    return json.loads(await asyncio.to_thread(native.ttv_plan_json, _json(request)))


async def worker_request(body: dict, environment: dict | None = None):
    native = native_module()
    environment = dict(os.environ) if environment is None else environment
    result = await asyncio.to_thread(native.ttv_worker_request_json,
                                    _json(body), _json(environment))
    return json.loads(result)


async def render(request: dict) -> tuple[bytes, dict]:
    native = native_module()
    cancellation = native.NativeRenderCancellation()
    task = asyncio.create_task(asyncio.to_thread(native.ttv_render_json,
                                                 _json(request), cancellation))
    try:
        audio, metadata = await asyncio.shield(task)
        return audio, json.loads(metadata)
    except asyncio.CancelledError:
        cancellation.cancel()
        # Cancellation is logical only until Rust has stopped and reaped the
        # native process group and released every source-analysis lease.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()
        raise


async def synthesize(body: dict, environment: dict | None = None):
    return await render(await worker_request(body, environment))


async def close():
    await asyncio.to_thread(native_module().ttv_close_residents)
