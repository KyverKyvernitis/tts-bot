"""Exercise the real routing/producer methods without speech providers or Discord."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def methods(relative, class_name, wanted, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    selected = [node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    assert {node.name for node in selected} == set(wanted)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / relative), "exec"), namespace)
    return {node.name: namespace[node.name] for node in selected}


AUDIO_METHODS = {
    "_tts_agent_route_state", "_tts_agent_public_snapshot", "_get_metrics_store",
    "_record_tts_agent_route_sample", "_observe_tts_route_decision", "_observe_tts_effective_route",
    "_record_route_engine_result", "_run_timed_generation", "_generate_audio_file",
    "_resolve_or_generate_singleflight_audio",
}
namespace = {
    "asyncio": asyncio, "contextlib": contextlib, "time": time,
    "logger": logging.getLogger(__name__), "TTS_WORKER_AGENT_ENABLED": True, "TTS_DEBUG_LOGS": False,
    "_has_speakable_tts_text": lambda text: any(letter.isalnum() for letter in text),
}
Base = type("RealAudioMethods", (), methods("cogs/tts/audio.py", "TTSAudioMixin", AUDIO_METHODS, namespace))


class Probe(Base):
    def __init__(self):
        self._tts_metrics = {}
        self.preference = (False, "worker_busy")
        self.agent_available = True
        self.worker_error = None
        self.local_error = None
        self.legacy_results = []
        self.persisted = []

    def _tts_agent_route_available(self):
        return self.agent_available

    def _tts_agent_should_try_worker(self, item):
        return self.preference if self.agent_available else (False, "worker_offline_or_not_ready")

    def _record_engine_success(self, engine, duration):
        self.legacy_results.append((engine, True))

    def _record_engine_failure(self, engine, error, *, duration_ms):
        self.legacy_results.append((engine, False))

    def _schedule_persistent_synt_success(self, guild_id, engine):
        self.persisted.append((guild_id, engine))

    async def _generate_tts_agent_worker_file(self, item):
        if self.worker_error:
            raise self.worker_error
        item._tts_agent_selected_engine = getattr(self, "selected_engine", item.engine)
        item._tts_worker_cache_hit = getattr(self, "worker_cache_hit", False)
        return "/tmp/worker.mp3"

    async def _generate_gtts_file_with_priority(self, *args, **kwargs):
        if self.local_error:
            raise self.local_error
        return "/tmp/local.mp3"

    def _cache_key(self, item):
        return "cache-key"

    def _try_get_cached_path(self, state, item):
        return "/tmp/cached.mp3"

    def _lease_audio_path(self, item, path):
        pass


def item(engine="gtts"):
    return SimpleNamespace(engine=engine, guild_id=77, text="Olá mundo", language="pt", voice="pt-BR", rate="+0%", pitch="+0Hz")


class FileRouteTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_independent_from_successful_local_choice(self):
        probe = Probe()
        health = probe._tts_agent_route_state()
        health.update(route="worker", ok=True)
        result = await probe._generate_audio_file(item())
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual(result, "/tmp/local.mp3")
        self.assertEqual((snapshot["route"], snapshot["ok"]), ("worker", True))
        self.assertEqual((snapshot["last_route_decision"], snapshot["last_route_reason"]), ("vps", "worker_busy"))
        self.assertEqual((snapshot["last_effective_route"], snapshot["last_effective_reason"]), ("vps", "worker_busy"))
        self.assertEqual(probe._tts_metrics["engines_by_route"]["vps"]["gtts"]["synth_count"], 1)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"], {})

    async def test_worker_failure_preserves_decision_and_records_local_fallback(self):
        probe = Probe()
        probe.preference = (True, "recent_first_audio")
        probe.worker_error = RuntimeError("worker unavailable")
        await probe._generate_audio_file(item())
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual((snapshot["last_route_decision"], snapshot["last_route_reason"]), ("worker", "recent_first_audio"))
        self.assertEqual((snapshot["last_effective_route"], snapshot["last_effective_reason"]), ("vps", "worker_fallback"))
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["gtts"]["synth_failures"], 1)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["gtts"]["synth_count"], 0)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["vps"]["gtts"]["synth_count"], 1)

    async def test_worker_success_attributes_selected_engine(self):
        probe = Probe()
        probe.preference = (True, "always_worker_engine")
        probe.selected_engine = "edge"
        await probe._generate_audio_file(item())
        self.assertEqual(probe._tts_agent_public_snapshot()["last_effective_route"], "worker")
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["edge"]["synth_count"], 1)
        self.assertNotIn("gtts", probe._tts_metrics["engines_by_route"]["worker"])
        self.assertEqual(probe.legacy_results, [("tts_agent:gtts", True)])

    async def test_failed_and_cancelled_generations_do_not_replace_last_success(self):
        probe = Probe()
        probe._observe_tts_effective_route(route="worker", reason="previous_success")
        probe.preference = (True, "recent_first_audio")
        probe.worker_error = RuntimeError("worker failure")
        probe.local_error = RuntimeError("local failure")
        with self.assertRaisesRegex(RuntimeError, "local failure"):
            await probe._generate_audio_file(item())
        self.assertEqual(probe._tts_agent_public_snapshot()["last_effective_reason"], "previous_success")
        probe.worker_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await probe._generate_audio_file(item())
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["gtts"]["synth_failures"], 1)
        self.assertEqual(probe._tts_agent_public_snapshot()["last_effective_reason"], "previous_success")

    async def test_cache_hit_and_invalid_text_do_not_invent_a_synthesis_route(self):
        probe = Probe()
        snapshot = probe._tts_agent_public_snapshot()
        for key in ("last_route_decision", "last_route_reason", "last_effective_route", "last_effective_reason"):
            self.assertEqual(snapshot[key], "")
        request = item()
        self.assertEqual(await probe._resolve_or_generate_singleflight_audio(None, request, read_cache=True, store_in_cache=True), ("/tmp/cached.mp3", False))
        self.assertEqual(request._tts_audio_origin, "cache")
        request.text = "..."
        with self.assertRaises(ValueError):
            await probe._generate_audio_file(request)
        self.assertNotIn("engines_by_route", probe._tts_metrics)
        self.assertEqual(probe._tts_agent_public_snapshot()["last_route_decision"], "")

    async def test_inline_worker_cache_has_no_new_synthesis_or_effective_route(self):
        probe = Probe()
        probe.preference = (True, "recent_first_audio")
        probe.worker_cache_hit = True
        probe._observe_tts_effective_route(route="vps", reason="previous_success")
        await probe._generate_audio_file(item())
        metric = probe._tts_metrics["engines_by_route"]["worker"]["gtts"]
        self.assertEqual((metric["synth_count"], metric["synth_total_ms"], metric["cache_hits"]), (0, 0.0, 1))
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual((snapshot["last_effective_route"], snapshot["last_effective_reason"]), ("vps", "previous_success"))

    async def test_health_engine_does_not_invent_last_synthesis_and_false_cache_is_valid(self):
        probe = Probe()
        state = probe._tts_agent_route_state()
        state.update(route="worker", ok=True, engine="android_native")
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual(snapshot["last_selected_engine"], "")
        self.assertIsNone(snapshot["last_cache_hit"])
        state["last_cache_hit"] = False
        self.assertIs(probe._tts_agent_public_snapshot()["last_cache_hit"], False)

    async def test_route_counter_clears_current_failure_streak_after_recovery(self):
        probe = Probe()
        probe._record_route_engine_result("gtts", "vps", 15, error=RuntimeError("temporary"))
        probe._record_route_engine_result("gtts", "vps", 25)
        metric = probe._tts_metrics["engines_by_route"]["vps"]["gtts"]
        self.assertEqual((metric["synth_count"], metric["synth_total_ms"], metric["synth_failures"], metric["consecutive_failures"]), (1, 25, 1, 0))
        self.assertEqual(metric["last_error"], "")


# The real stream methods import audio lazily; isolate that dependency in a
# private package so these tests cannot replace the application's modules.
stream_ns = {"__name__": "telemetry_test.streaming", "__package__": "telemetry_test", "asyncio": asyncio, "contextlib": contextlib, "time": time}
stream_tree = ast.parse((ROOT / "cogs/tts/streaming.py").read_text(encoding="utf-8"))
stream_iterator = next(node for node in stream_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_iter_with_deadline")
exec(compile(ast.Module(body=[stream_iterator], type_ignores=[]), str(ROOT / "cogs/tts/streaming.py"), "exec"), stream_ns)
StreamBase = type("RealStreamMethods", (), methods("cogs/tts/streaming.py", "SharedSynthesisMixin", {"_produce_shared_job", "_produce_worker_stream_job"}, stream_ns))


class Buffer:
    def __init__(self):
        self.size = 0
        self.error = None

    async def finish(self, error=None):
        self.error = error


class Semaphore:
    async def acquire(self, **kwargs):
        pass

    def release(self):
        pass


class StreamProbe(Probe, StreamBase):
    def __init__(self):
        super().__init__()
        self.stream_available = False
        self.failed_requests = []
        self.response = None

    def _get_synth_semaphore(self):
        return Semaphore()

    async def _shared_prefetch_slot(self, job):
        return None

    def _record_latency_sample(self, *args):
        pass

    def _worker_stream_available_for(self, request):
        return self.stream_available

    async def _append_shared_audio(self, job, data):
        job.buffer.size += len(data)
        job.first_audio_ms = 1.0
        return True

    def _normalize_edge_rate(self, rate):
        return rate

    def _normalize_edge_pitch(self, pitch):
        return pitch

    def _route_measurements(self):
        return SimpleNamespace(record=lambda *args, **kwargs: None)

    def _phone_worker_tts_base_url(self):
        return "http://worker.test"

    def _tts_agent_payload_for_item(self, request):
        return {"text": request.text}

    async def _get_phone_worker_http_session(self):
        return SimpleNamespace(post=lambda *args, **kwargs: self.response)

    def _worker_header_value(self, headers, key, default=""):
        return headers.get(key, default)

    def _record_tts_agent_synth_success(self, **kwargs):
        pass

    def _mark_tts_agent_synth_failure(self, error):
        self.failed_requests.append(error)


def stream_job():
    return SimpleNamespace(item=item("edge"), route="local", foreground=asyncio.Event(), stop=asyncio.Event(),
        started_at=time.monotonic(), slot_ready=asyncio.Event(), buffer=Buffer(), actual_engine="edge", first_audio_ms=0.0, store_in_cache=False)


class WorkerResponse:
    status = 200
    headers = {"X-Core-Worker-Stream-Protocol": "2", "X-Core-Worker-Engine": "edge", "X-Core-Worker-Audio-Format": "mp3"}

    def __init__(self, *, partial=False, fail=False, cached=False):
        self.partial = partial
        self.fail = fail
        self.headers = {**type(self).headers, "X-Core-Worker-Cache-Hit": "true" if cached else "false"}
        self.content = SimpleNamespace(iter_chunked=self.chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def chunks(self, size):
        if self.partial or not self.fail:
            yield b"audio"
        if self.fail:
            raise RuntimeError("stream disconnected")


class StreamRouteTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        package = types.ModuleType("telemetry_test")
        package.__path__ = []
        audio = types.ModuleType("telemetry_test.audio")
        audio.TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS = 10
        audio.TTS_EDGE_CONNECT_TIMEOUT_SECONDS = 2
        audio.TTS_EDGE_RECEIVE_TIMEOUT_SECONDS = 2
        audio.TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS = 10
        audio.PHONE_WORKER_TOKEN = "fixture-token"
        audio.aiohttp = SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs)
        audio.validate_voice = lambda voice, names: voice

        class LocalProvider:
            def __init__(self, **kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"local audio"}

        audio.edge_tts = SimpleNamespace(Communicate=LocalProvider)
        package.audio = audio
        self.modules = patch.dict(sys.modules, {"telemetry_test": package, "telemetry_test.audio": audio})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    async def test_worker_stream_success_counts_only_worker_engine(self):
        probe = StreamProbe()
        probe.preference = (True, "recent_first_audio")
        probe.stream_available = True
        probe.response = WorkerResponse()
        job = stream_job()
        await probe._produce_shared_job(job)
        self.assertIsNone(job.buffer.error)
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual((snapshot["last_route_decision"], snapshot["last_effective_route"]), ("worker", "worker"))
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["edge"]["synth_count"], 1)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["vps"], {})

    async def test_worker_stream_failure_before_audio_records_vps_fallback(self):
        probe = StreamProbe()
        probe.preference = (True, "recent_first_audio")
        probe.stream_available = True
        probe.response = WorkerResponse(fail=True)
        probe._edge_stream_audio_chunk = lambda message: message["data"]
        job = stream_job()
        await probe._produce_shared_job(job)
        self.assertIsNone(job.buffer.error)
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual(snapshot["last_route_decision"], "worker")
        self.assertEqual((snapshot["last_effective_route"], snapshot["last_effective_reason"]), ("vps", "worker_fallback"))
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["edge"]["synth_failures"], 1)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["vps"]["edge"]["synth_count"], 1)

    async def test_inline_cached_worker_stream_is_counted_as_cache_instead_of_synthesis(self):
        probe = StreamProbe()
        probe.preference = (True, "recent_first_audio")
        probe.stream_available = True
        probe.response = WorkerResponse(cached=True)
        probe._observe_tts_effective_route(route="vps", reason="previous_success")
        job = stream_job()
        await probe._produce_shared_job(job)
        self.assertIsNone(job.buffer.error)
        metric = probe._tts_metrics["engines_by_route"]["worker"]["edge"]
        self.assertEqual((metric["synth_count"], metric["synth_total_ms"], metric["cache_hits"]), (0, 0.0, 1))
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual((snapshot["last_effective_route"], snapshot["last_effective_reason"]), ("vps", "previous_success"))

    async def test_partial_worker_stream_is_never_reported_as_success_or_retried_locally(self):
        probe = StreamProbe()
        probe.preference = (True, "recent_first_audio")
        probe.stream_available = True
        probe.response = WorkerResponse(partial=True, fail=True)
        probe._observe_tts_effective_route(route="vps", reason="previous_success")
        job = stream_job()
        await probe._produce_shared_job(job)
        self.assertIsInstance(job.buffer.error, RuntimeError)
        self.assertEqual(probe._tts_agent_public_snapshot()["last_effective_reason"], "previous_success")
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["edge"]["synth_failures"], 1)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["worker"]["edge"]["synth_count"], 0)
        self.assertEqual(probe._tts_metrics["engines_by_route"]["vps"], {})

    async def test_local_stream_preserves_healthy_worker_but_records_vps(self):
        probe = StreamProbe()
        probe._edge_stream_audio_chunk = lambda message: message["data"]
        probe._tts_agent_route_state().update(route="worker", ok=True)
        job = stream_job()
        await probe._produce_shared_job(job)
        self.assertIsNone(job.buffer.error)
        snapshot = probe._tts_agent_public_snapshot()
        self.assertEqual((snapshot["route"], snapshot["ok"]), ("worker", True))
        self.assertEqual((snapshot["last_route_decision"], snapshot["last_effective_route"]), ("vps", "vps"))
        self.assertEqual(snapshot["last_effective_reason"], "worker_busy")
