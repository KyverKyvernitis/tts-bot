"""Standalone latency/admission regression tests (standard library + aiohttp).

Run: python -m unittest discover -s tests -p test_ttv_latency_admission.py
Uses the real worker facade/provider and the bot's raw decoder; no pytest,
Discord SDK, voicebank, synthesis binary, network connection or login required.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch
import wave

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"
MEASUREMENTS = {
    "resample_ms": 125.25, "compose_ms": 8.5, "postprocess_ms": 4.0,
    "cache_maintenance_ms": 1.25, "render_workers": 2, "source_groups": 3,
    "fragment_cache_hits": 9, "fragment_cache_misses": 4, "resampler_calls": 4,
    "cache_scans": 0, "cache_evictions": 0, "cache_size_bytes": 2048,
}


def pcm(duration_ms=400):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as target:
        target.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        target.writeframes(b"\x01\x00" * round(44100 * duration_ms / 1000))
    return stream.getvalue()


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback):
        self.jobs.append(callback)


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ttv-latency-admission-")
        self.addCleanup(self.directory.cleanup)
        values = {key: value for key, value in os.environ.items()
                  if not key.startswith(("PHONE_WORKER_TTS", "PHONE_WORKER_TETO", "PHONE_WORKER_ANDROID_TTS"))}
        values.update(CORE_WORKER_PROFILE="turbo", CORE_WORKER_ROLES="cache-worker",
            CORE_WORKER_CAPABILITIES="cache-worker,tts-synth", CORE_WORKER_ID="latency-fixture",
            PHONE_WORKER_TTS_CACHE_DIR=str(Path(self.directory.name) / "cache"),
            CORE_WORKER_APK_DIR=str(Path(self.directory.name) / "apk"))
        environment = patch.dict(os.environ, values, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location("phone_latency_unittest", PHONE)
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)
        self.handler = object.__new__(self.worker.WorkerHandler)
        self.handler.server = SimpleNamespace(job_timeout=30, max_output_bytes=8 * 1024 * 1024,
                                             max_body_bytes=1024 * 1024)
        self.worker.time = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 1000.0)
        self.worker._TTS_CACHE_MAINTENANCE_EXECUTOR = DeferredExecutor()
        deps = {"edge_tts": False, "gtts": False, "android_native_tts": False, "teto_tts": True,
                "teto": {"fingerprint": "latency-fixture-bank", "ready": True, "enabled": True,
                         "resources": {"ok": True}}}
        self.worker._turbo_dependency_snapshot = lambda: dict(deps)
        self.worker._teto_status = lambda: dict(deps["teto"])
        self.renderer = SimpleNamespace(synthesize_performance=self.perform,
            SUPPORTS_BACKGROUND_CANCELLATION=True, supports_background_cancellation=lambda: True,
            status=lambda **kwargs: {"ready": True, "available": True, "ttv_languages": ["pt-BR", "en-US"]})
        self.worker._get_teto_renderer = lambda: self.renderer
        self.alterations = {}
        self.calls = []

    def body(self, **changes):
        body = {"engine": "teto", "text": "aaa", "ttv_voice_id": "kasane-teto",
                "ttv_language": "pt-BR", "ttv_pitch_semitones": 0.0, "ttv_speech_rate": 1.0,
                "ttv_score": {"version": 1, "track_index": 0, "duration_ms": 400,
                    "lyrics_source": "message", "notes": [{"midi_note": 60, "start_ms": 0,
                    "duration_ms": 400, "velocity": 90, "lyric": "a"}]}, **changes}
        body["ttv_performance_hash"] = self.worker._phone_worker_tts_policy_module().performance_request(body)["hash"]
        return body

    def perform(self, text, **kwargs):
        self.assertTrue(self.worker._HEAVY_RESOURCE_LOCK.locked())
        self.calls.append({"text": text, **kwargs})
        identity = self.worker._phone_worker_tts_policy_module().performance_request({
            "engine": "teto", "text": text, "ttv_score": kwargs["score"],
            "ttv_segments": kwargs["segments"]})["hash"]
        return pcm(), {"voice_id": "kasane-teto", "audio_format": "wav",
            "pitch_offset_semitones": kwargs["pitch_offset_semitones"],
            "speech_rate": kwargs["speech_rate"], "reading_language": kwargs["reading_language"],
            "ttv_performance_hash": identity, **self.alterations}


class LatencyTelemetryTests(WorkerFixture):
    def test_json_and_raw_envelopes_preserve_metrics_and_do_not_replay_cache_work(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                self.alterations.update(MEASUREMENTS, private_telemetry="not-projected")
                body = self.body(text="raw" if raw else "json")
                cold = self.handler._task_tts_agent_synthesize(body, raw_response=raw)
                self.assertEqual({key: cold[key] for key in MEASUREMENTS}, MEASUREMENTS)
                self.assertNotIn("private_telemetry", cold)
                self.assertEqual(cold["timing_ms"]["teto_admission"], 0)
                warm = self.handler._task_tts_agent_synthesize(body, raw_response=raw)
                self.assertTrue(warm["cache_hit"])
                self.assertFalse(set(MEASUREMENTS).intersection(warm))
                self.assertEqual(warm["sha256"], cold["sha256"])

    def test_invalid_measurements_are_rejected_and_zero_is_preserved(self):
        project = self.worker._phone_worker_tts_providers_module().render_telemetry
        for invalid in (float("nan"), float("inf"), -1, True, "12", 10 ** 1000):
            with self.subTest(value=type(invalid).__name__):
                self.assertEqual(project({key: invalid for key in MEASUREMENTS}), {})
        self.assertEqual(project({"resample_ms": 0, "resampler_calls": 0, "source_groups": 2.5,
                                  "fragment_cache_hits": 3.0, "private": 1}),
                         {"resample_ms": 0.0, "resampler_calls": 0, "fragment_cache_hits": 3})

    def test_real_header_writer_and_bot_raw_decoder_current_and_legacy(self):
        tree = ast.parse((ROOT / "cogs/tts/audio.py").read_text())
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSAudioMixin")
        wanted = {"_request_phone_worker_raw_audio", "_normalize_worker_audio_format",
                  "_worker_header_value", "_parse_header_float", "_parse_header_bool"}
        methods = [node for node in owner.body if getattr(node, "name", "") in wanted]
        klass = ast.ClassDef(name="Transport", bases=[], keywords=[], body=methods, decorator_list=[])
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), klass], type_ignores=[])
        namespace = dict(globals(), PHONE_WORKER_TOKEN="latency-fixture-token")
        exec(compile(ast.fix_missing_locations(module), "production_bot_transport", "exec"), namespace)
        Transport = namespace["Transport"]

        class Capture:
            def __init__(self):
                self.headers = Message()
                self.wfile = io.BytesIO()
            def send_response(self, code):
                self.code = code
            def send_header(self, name, value):
                self.headers[name] = value
            def end_headers(self):
                pass

        for current in (True, False):
            self.alterations = dict(MEASUREMENTS) if current else {}
            result = self.handler._task_tts_agent_synthesize(
                self.body(text="current" if current else "legacy"), raw_response=True)
            capture = Capture()
            self.worker._audio_response(capture, 200, result["_raw_audio"], result)

            class Response:
                status = 200
                headers = capture.headers
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    return None
                async def read(self):
                    return capture.wfile.getvalue()

            class Probe(Transport):
                def _phone_worker_tts_base_url(self):
                    return "http://local-fixture"
                async def _get_phone_worker_http_session(self):
                    return SimpleNamespace(post=lambda *args, **kwargs: Response())

            decoded = asyncio.run(Probe()._request_phone_worker_raw_audio(
                payload={}, timeout_seconds=3, max_audio_mb=8, stream_to_file=False))
            self.assertEqual(decoded["raw_audio"], pcm())
            for key, value in MEASUREMENTS.items():
                if current:
                    self.assertEqual(decoded[key], value)
                else:
                    self.assertNotIn(key, decoded)
            self.assertTrue({"preflight", "dependency_snapshot", "engine_order", "cache_key",
                "cache_lookup", "teto_render", "teto_admission"} <= decoded["timing_ms"].keys())


class BackgroundAdmissionTests(WorkerFixture):
    def test_foreground_preempts_background_with_concurrency_one_after_physical_drain(self):
        os.environ["PHONE_WORKER_TTS_AGENT_CONCURRENCY"] = "1"
        entered, saw_cancel, drain, foreground_entered = (threading.Event() for _ in range(4))
        active, guard = [], threading.Lock()
        normal_perform = self.perform

        def perform(text, **kwargs):
            with guard:
                active.append(text)
                self.assertEqual(len(active), 1, "physical renders overlapped")
            try:
                cancelled = kwargs.get("cancelled")
                if cancelled is not None:
                    entered.set()
                    deadline = time.monotonic() + 3
                    while not cancelled() and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertTrue(cancelled(), "foreground did not signal cancellation")
                    saw_cancel.set()
                    self.assertTrue(drain.wait(2), "test did not release physical background render")
                    raise RuntimeError("renderização TTV cancelada")
                foreground_entered.set()
                return normal_perform(text, **kwargs)
            finally:
                with guard:
                    active.remove(text)

        self.renderer.synthesize_performance = perform
        body = self.body(cache_mode="bypass")
        with ThreadPoolExecutor(max_workers=2) as pool:
            background = pool.submit(self.handler._task_tts_agent_synthesize, {**body, "priority": "background"})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.worker._TTS_AGENT_ACTIVE, 0)
                self.assertEqual(self.worker._TTS_AGENT_BACKGROUND_ACTIVE, 1)
                foreground = pool.submit(self.handler._task_tts_agent_synthesize, body)
                self.assertTrue(saw_cancel.wait(2))
                self.assertFalse(foreground_entered.wait(.05))
                state = self.worker._TETO_BACKGROUND_STATE
                self.assertEqual(state["foreground_waiters"], 1)
                with self.assertRaisesRegex(RuntimeError, "ocupado"):
                    self.worker._phone_worker_tts_providers_module()._acquire_teto_render(
                        self.worker._HEAVY_RESOURCE_LOCK, state, background=True)
                drain.set()
                with self.assertRaisesRegex(RuntimeError, "cancelada"):
                    background.result(timeout=3)
                self.assertTrue(foreground.result(timeout=3)["ok"])
            finally:
                drain.set()
        self.assertFalse(self.worker._HEAVY_RESOURCE_LOCK.locked())
        self.assertIsNone(self.worker._TETO_BACKGROUND_STATE["cancelled"])
        self.assertEqual(self.worker._TETO_BACKGROUND_STATE["foreground_waiters"], 0)
        self.assertEqual(self.worker._TTS_AGENT_ACTIVE, 0)
        self.assertEqual(self.worker._TTS_AGENT_BACKGROUND_ACTIVE, 0)
        self.assertEqual(self.worker._TTS_AGENT_TOTAL, 1)
        self.assertEqual(self.worker._TTS_AGENT_FAILED, 0)
        self.assertEqual(self.worker._TTS_AGENT_BACKGROUND_ABORTED, 1)
        self.assertEqual(self.worker._TTS_AGENT_LAST_ERROR, "")

    def test_unverified_background_is_declined_without_loading_audio(self):
        self.renderer.supports_background_cancellation = lambda: False
        with self.assertRaisesRegex(RuntimeError, "sem suporte a cancelamento"):
            self.handler._task_tts_agent_synthesize(self.body(priority="background"))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worker._HEAVY_RESOURCE_LOCK.locked())
        self.assertIsNone(self.worker._TETO_BACKGROUND_STATE["cancelled"])
        self.assertEqual(self.worker._TTS_AGENT_LAST_ERROR, "")

    def test_foreground_timeout_does_not_clear_other_owner(self):
        event = threading.Event()
        state = {"lock": threading.Lock(), "cancelled": event, "foreground_waiters": 0}
        owner = self
        class HeldLock:
            def acquire(self, **kwargs):
                return False
            def release(self):
                owner.fail("must not release a lock that was never acquired")
        with self.assertRaisesRegex(RuntimeError, "cancelamento.*pendente"):
            self.worker._phone_worker_tts_providers_module()._acquire_teto_render(HeldLock(), state)
        self.assertTrue(event.is_set())
        self.assertIs(state["cancelled"], event)
        self.assertEqual(state["foreground_waiters"], 0)


if __name__ == "__main__":
    unittest.main()
