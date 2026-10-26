"""Singing measurements and background admission through the real HTTP facade."""
from __future__ import annotations

import ast
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401
from test_ttv_performance_worker import performer, request, score, wav_bytes  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
METRICS = {
    "resample_ms": 125.25, "compose_ms": 8.5, "postprocess_ms": 4.0,
    "cache_maintenance_ms": 1.25, "render_workers": 2, "source_groups": 3,
    "fragment_cache_hits": 9, "fragment_cache_misses": 4, "resampler_calls": 4,
    "cache_scans": 0, "cache_evictions": 0, "cache_size_bytes": 2048,
}


def bot_transport():
    """Load production transport methods without requiring a Discord login/SDK."""
    tree = ast.parse((ROOT / "cogs/tts/audio.py").read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSAudioMixin")
    names = {"_request_phone_worker_raw_audio", "_normalize_worker_audio_format",
             "_worker_header_value", "_parse_header_float", "_parse_header_bool"}
    methods = [node for node in owner.body if getattr(node, "name", "") in names]
    namespace = dict(globals(), PHONE_WORKER_TOKEN="latency-local-test-token")
    klass = ast.ClassDef(name="Transport", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), klass], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "real_ttv_transport", "exec"), namespace)
    return namespace["Transport"]


@pytest.mark.parametrize("raw", [False, True])
def test_renderer_measurements_survive_success_envelope(tts, performer, raw):
    performer.alterations.update(METRICS, private_telemetry="never-project")
    result = tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()), raw_response=raw)
    for key, value in METRICS.items():
        assert result[key] == value
    assert "private_telemetry" not in result
    assert result["timing_ms"]["teto_admission"] == 0
    warm = tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()), raw_response=raw)
    assert warm["cache_hit"]
    assert not set(METRICS).intersection(warm), "cache hits must not report old render work"
    assert audio(result) == audio(warm)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "12", 10 ** 1000])
def test_invalid_renderer_measurements_are_not_projected(tts, performer, invalid):
    performer.alterations.update({key: invalid for key in METRICS})
    result = tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()))
    assert not set(METRICS).intersection(result)


def test_fractional_counts_are_rejected_and_zero_timings_preserved(tts):
    projection = tts.worker._phone_worker_tts_providers_module().render_telemetry({
        "resample_ms": 0, "source_groups": 2.5, "resampler_calls": 0,
        "fragment_cache_hits": 3.0, "private": 10,
    })
    assert projection == {"resample_ms": 0.0, "resampler_calls": 0, "fragment_cache_hits": 3}


@pytest.mark.parametrize("with_metrics", [True, False], ids=["current", "legacy"])
def test_raw_http_bot_consumer_preserves_stage_and_fragment_measurements(tts, performer, with_metrics):
    if with_metrics:
        performer.alterations.update(METRICS)
    server = ThreadingHTTPServer(("127.0.0.1", 0), tts.worker.WorkerHandler)
    server.daemon_threads = True
    server.worker_token = "latency-local-test-token"
    server.max_body_bytes = 1024 * 1024
    server.max_output_bytes = 8 * 1024 * 1024
    server.job_timeout = 30
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    Transport = bot_transport()

    class Probe(Transport):
        def _phone_worker_tts_base_url(self):
            return f"http://127.0.0.1:{server.server_port}"

        async def _get_phone_worker_http_session(self):
            return self.session

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = Probe()
            probe.session = session
            result = await probe._request_phone_worker_raw_audio(
                payload=request(tts, ttv_score=score()), timeout_seconds=5,
                max_audio_mb=8, stream_to_file=False)
            assert result["raw_audio"] == wav_bytes(400)
            for key, value in METRICS.items():
                if with_metrics:
                    assert result[key] == value
                else:
                    assert key not in result
            assert {"preflight", "dependency_snapshot", "engine_order", "cache_key",
                    "cache_lookup", "teto_admission", "teto_render", "cache_store"} <= result["timing_ms"].keys()
            assert all(value >= 0 for value in result["timing_ms"].values())

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_foreground_preempts_background_even_with_one_foreground_slot(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TTS_AGENT_CONCURRENCY", "1")
    tts.handler.server.max_output_bytes = 8 * 1024 * 1024
    entered, saw_cancel, drain, foreground_entered = (threading.Event() for _ in range(4))
    active = []
    guard = threading.Lock()
    body = request(tts, ttv_score=score(), cache_mode="bypass")

    def perform(text, **kwargs):
        with guard:
            active.append(text)
            assert len(active) == 1, "physical renders must never overlap"
        try:
            cancelled = kwargs.get("cancelled")
            if cancelled is not None:
                entered.set()
                deadline = time.monotonic() + 3
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(.005)
                assert cancelled(), "foreground failed to request cancellation"
                saw_cancel.set()
                assert drain.wait(2), "test did not release physical background renderer"
                raise RuntimeError("renderização TTV cancelada")
            foreground_entered.set()
            return wav_bytes(400), {
                "voice_id": "kasane-teto", "audio_format": "wav",
                "pitch_offset_semitones": 0.0, "speech_rate": 1.0,
                "reading_language": "pt-BR", "ttv_performance_hash": body["ttv_performance_hash"],
            }
        finally:
            with guard:
                active.remove(text)

    renderer = SimpleNamespace(synthesize_performance=perform,
        SUPPORTS_BACKGROUND_CANCELLATION=True, supports_background_cancellation=lambda: True)
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(tts.handler._task_tts_agent_synthesize, {**body, "priority": "background"})
        try:
            assert entered.wait(2)
            assert tts.worker._TTS_AGENT_ACTIVE == 0
            assert tts.worker._TTS_AGENT_BACKGROUND_ACTIVE == 1
            foreground = pool.submit(tts.handler._task_tts_agent_synthesize, body)
            assert saw_cancel.wait(2)
            assert not foreground_entered.wait(.05), "foreground started before physical cleanup"
            state = tts.worker._TETO_BACKGROUND_STATE
            assert state["foreground_waiters"] == 1
            # Another speculative render cannot overtake the waiting user.
            with pytest.raises(RuntimeError, match="ocupado"):
                tts.worker._phone_worker_tts_providers_module()._acquire_teto_render(
                    tts.worker._HEAVY_RESOURCE_LOCK, state, background=True)
            drain.set()
            with pytest.raises(RuntimeError, match="cancelada"):
                background.result(timeout=3)
            assert audio(foreground.result(timeout=3)) == wav_bytes(400)
        finally:
            drain.set()
    assert foreground_entered.is_set()
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert tts.worker._TETO_BACKGROUND_STATE["cancelled"] is None
    assert tts.worker._TETO_BACKGROUND_STATE["foreground_waiters"] == 0
    assert tts.worker._TTS_AGENT_ACTIVE == tts.worker._TTS_AGENT_BACKGROUND_ACTIVE == 0
    assert tts.worker._TTS_AGENT_TOTAL == 1 and tts.worker._TTS_AGENT_FAILED == 0
    assert tts.worker._TTS_AGENT_BACKGROUND_TOTAL == tts.worker._TTS_AGENT_BACKGROUND_ABORTED == 1
    assert tts.worker._TTS_AGENT_LAST_ERROR == ""


def test_background_rejects_unverified_renderer_and_preserves_heavy_ownership(tts, performer):
    body = request(tts, ttv_score=score(), priority="background")
    with pytest.raises(RuntimeError, match="sem suporte a cancelamento"):
        tts.handler._task_tts_agent_synthesize(body)
    assert performer.calls == []
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert tts.worker._TETO_BACKGROUND_STATE["cancelled"] is None
    assert tts.worker._TTS_AGENT_ACTIVE == tts.worker._TTS_AGENT_BACKGROUND_ACTIVE == 0
    assert tts.worker._TTS_AGENT_LAST_ERROR == ""


def test_foreground_timeout_keeps_background_owner_and_releases_waiter(tts):
    module = tts.worker._phone_worker_tts_providers_module()
    event = threading.Event()
    state = {"lock": threading.Lock(), "cancelled": event, "foreground_waiters": 0}

    class HeldLock:
        def acquire(self, **kwargs):
            return False

        def release(self):
            pytest.fail("caller must not release a lock it never acquired")

    with pytest.raises(RuntimeError, match="cancelamento.*pendente"):
        module._acquire_teto_render(HeldLock(), state)
    assert event.is_set() and state["cancelled"] is event
    assert state["foreground_waiters"] == 0


@pytest.mark.parametrize("background", [False, True])
def test_background_abort_does_not_retry_or_suspend_bot_route(background):
    tree = ast.parse((ROOT / "cogs/tts/audio.py").read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSAudioMixin")
    method = next(node for node in owner.body if getattr(node, "name", "") == "_generate_tts_agent_worker_file")
    import logging
    import re
    namespace = dict(globals(), logger=logging.getLogger(__name__), re=re,
        frozen_arrangement=lambda item: None, _ttv_performance_payload=lambda item: {},
        TTS_WORKER_AGENT_MAX_TEXT_LENGTH=2048, TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS=2,
        TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS=0, TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS=2,
        TTS_WORKER_AGENT_MAX_AUDIO_MB=8)
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "production_tts_generator", "exec"), namespace)

    async def run():
        attempts, failures = [], []

        async def unavailable(**kwargs):
            attempts.append(kwargs)
            raise RuntimeError("busy; background cancelled")

        owner = SimpleNamespace(
            _tts_agent_route_available=lambda: True, _tts_agent_route_state=lambda: {},
            _get_metrics_store=lambda: {}, _tts_agent_payload_for_item=lambda item: {},
            _request_phone_worker_tts_audio=unavailable,
            _is_tts_agent_transient_busy_error=lambda exc: True,
            _mark_tts_agent_synth_failure=lambda exc: failures.append(exc))
        item = SimpleNamespace(engine="gtts", text="hello", ttv_score=None, guild_id=1,
                               _cd_background=background)
        with pytest.raises(RuntimeError, match="background cancelled"):
            await namespace["_generate_tts_agent_worker_file"](owner, item)
        assert len(attempts) == (1 if background else 3)
        assert len(failures) == (0 if background else 1)

    asyncio.run(run())


def test_raw_header_writer_filters_invalid_optional_measurements(tts):
    class Capture:
        def __init__(self):
            self.headers = {}
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.code = code

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

    capture = Capture()
    tts.worker._audio_response(capture, 200, b"audio", {
        "audio_format": "wav", "resample_ms": float("nan"),
        "compose_ms": float("inf"), "source_groups": -1,
        "render_workers": True, "resampler_calls": 1.5,
        "cache_scans": "2\r\nInjected: yes", "cache_evictions": 0,
        "postprocess_ms": 0,
    })
    projected = {name: value for name, value in capture.headers.items() if name.startswith("X-Core-Worker-TTV-")}
    assert projected == {"X-Core-Worker-TTV-Postprocess-Ms": "0.0", "X-Core-Worker-TTV-Cache-Evictions": "0"}
    assert capture.wfile.getvalue() == b"audio"


def test_pending_background_cleanup_is_a_transient_busy_error():
    tree = ast.parse((ROOT / "cogs/tts/audio.py").read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSAudioMixin")
    method = next(node for node in owner.body if getattr(node, "name", "") == "_is_tts_agent_transient_busy_error")
    namespace = {}
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "production_busy_classifier", "exec"), namespace)
    classify = namespace["_is_tts_agent_transient_busy_error"]
    assert classify(None, RuntimeError("recurso pesado ocupado; cancelamento do aquecimento TTV pendente"))
    assert not classify(None, RuntimeError("partitura inválida"))


def test_foreground_cache_hit_does_not_interrupt_physical_background(tts, performer):
    body = request(tts, ttv_score=score())
    cold = tts.handler._task_tts_agent_synthesize(body)
    entered, release = threading.Event(), threading.Event()

    def hold(text, **kwargs):
        assert kwargs.get("cancelled") is not None
        entered.set()
        assert release.wait(2)
        raise RuntimeError("test background finished")

    performer.renderer.SUPPORTS_BACKGROUND_CANCELLATION = True
    performer.renderer.supports_background_cancellation = lambda: True
    performer.renderer.synthesize_performance = hold
    other = request(tts, text="aaaa", ttv_score=score(), priority="background", cache_mode="bypass")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(tts.handler._task_tts_agent_synthesize, other)
        try:
            assert entered.wait(2)
            event = tts.worker._TETO_BACKGROUND_STATE["cancelled"]
            warm = tts.handler._task_tts_agent_synthesize(body)
            assert warm["cache_hit"] and audio(warm) == audio(cold)
            assert tts.worker._TETO_BACKGROUND_STATE["cancelled"] is event
            assert not event.is_set(), "cached user playback requires no heavy renderer admission"
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="test background finished"):
            pending.result(timeout=3)
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
