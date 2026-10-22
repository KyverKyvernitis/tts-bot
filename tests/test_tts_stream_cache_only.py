"""Repeated transport probes cannot synthesize on a cache miss or bad file."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = load("cache_only_stream_transport", PHONE / "tts_transport.py")
policy = load("cache_only_stream_policy", PHONE / "phone_worker_runtime/tts_policy.py")


class Handler:
    job_timeout = 5
    max_output_bytes = 65536

    def __init__(self, root):
        self.root = root
        self.headers = {}
        self.wfile = io.BytesIO()
        self.connection = SimpleNamespace(settimeout=lambda value: None)
        self.close_connection = False
        self.status = None
        self.cache_enabled = True
        self.touched = []

    def _ensure_tts_piper_turbo_allowed(self):
        return [], ["tts-synth", "cache-worker"]

    def _tts_agent_standard_cache_enabled(self, *_):
        return self.cache_enabled

    def _tts_agent_standard_cache_key(self, body, *, engine):
        # Exercise the real existing policy; engine forms part of the default
        # key, so an Edge request cannot fall through to a gTTS cache key.
        return policy.standard_cache_key(
            body, engine=engine, sanitize_key=policy.sanitize_cache_key,
            normalize_rate=lambda value: value or "+0%",
            normalize_pitch=lambda value: value or "+0Hz",
            normalize_language=lambda value: value or "pt",
            teto_fingerprint="unavailable", teto_base_pitch="C4",
        )

    _tts_agent_cache_mode_allows_read = staticmethod(policy.cache_mode_allows_read)
    _tts_agent_cache_mode_allows_store = staticmethod(policy.cache_mode_allows_store)
    _normalize_tts_gtts_language = staticmethod(lambda value: value or "pt")
    _normalize_tts_edge_rate = staticmethod(lambda value: value or "+0%")
    _normalize_tts_edge_pitch = staticmethod(lambda value: value or "+0Hz")

    def _find_tts_cache_file(self, key):
        for fmt in ("mp3", "wav"):
            path = self.root / (key + "." + fmt)
            if path.exists():
                return path, fmt
        return None, ""

    def _touch_tts_cache_file(self, path):
        self.touched.append(path)

    def _tts_cache_path(self, key, fmt):
        return self.root / (key + "." + fmt)

    def _schedule_tts_cache_prune(self, **kwargs):
        pass

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


class CacheOnlyStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.handler = Handler(self.root)
        self.done = []
        self.json_responses = []
        self.starts = 0

        def start():
            self.starts += 1

        def respond(handler, status, payload):
            handler.status = status
            self.json_responses.append((status, payload))

        self.api = SimpleNamespace(
            _env_bool=lambda *args: True,
            _env_int=lambda *args: 1200,
            _tts_agent_record_start=start,
            _tts_agent_record_done=lambda **kwargs: self.done.append(kwargs),
            _json_response=respond,
            _header_ascii=str,
            _default_worker_id=lambda: "phone-cache-test",
            PHONE_WORKER_VERSION="test",
        )
        self.provider = mock.Mock(side_effect=AssertionError("cache-only invoked a speech provider"))
        patcher = mock.patch.object(transport, "AudioStream", self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, **overrides):
        body = {"text": "Diagnóstico Osaka.", "engine": "edge", "cache_mode": "only"}
        body.update(overrides)
        return body

    def put(self, body, audio=b"cached-mp3-audio", *, engine=None, fmt="mp3"):
        key = self.handler._tts_agent_standard_cache_key(body, engine=engine or body["engine"])
        path = self.root / (key + "." + fmt)
        path.write_bytes(audio)
        return path

    def serve(self, body):
        transport.serve_stream(self.handler, body, self.api)
        self.provider.assert_not_called()
        self.assertEqual(self.starts, 1)
        self.assertEqual(len(self.done), 1)

    def test_cache_miss_returns_conflict_without_creating_provider_or_file(self):
        self.serve(self.request())
        self.assertEqual(self.handler.status, 409)
        self.assertEqual(self.json_responses[0][1]["error"], "cache_miss")
        self.assertFalse(self.done[0]["ok"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hit_streams_existing_audio_with_existing_protocol_and_identity(self):
        body = self.request()
        self.put(body, b"cached" * 4000)
        self.serve(body)
        self.assertEqual(self.handler.status, 200)
        self.assertEqual(self.handler.headers["X-Core-Worker-Stream-Protocol"], "2")
        self.assertEqual(self.handler.headers["X-Core-Worker-Engine"], "edge")
        self.assertEqual(self.handler.headers["X-Core-Worker-Cache-Hit"], "true")
        self.assertEqual(self.handler.headers["X-Core-Worker-Id"], os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "phone-cache-test")
        self.assertTrue(self.handler.wfile.getvalue().endswith(b"0\r\n\r\n"))
        self.assertTrue(self.done[0]["ok"])
        self.assertFalse(self.json_responses)

    def test_boolean_cache_only_flag_cannot_fall_back_to_provider(self):
        self.serve(self.request(cache_mode="prefer", cache_only=True))
        self.assertEqual(self.handler.status, 409)

    def test_string_cache_only_flag_cannot_fall_back_to_provider(self):
        self.serve(self.request(cache_mode="prefer", cache_only="true"))
        self.assertEqual(self.handler.status, 409)

    def test_disabled_cache_rejects_only_mode_without_provider(self):
        self.handler.cache_enabled = False
        self.serve(self.request())
        self.assertEqual(self.handler.status, 409)

    def test_explicit_bypass_with_only_flag_rejects_without_read_or_provider(self):
        body = self.request(cache_mode="bypass", cache_only=True)
        self.put(body)
        self.serve(body)
        self.assertEqual(self.handler.status, 409)
        self.assertFalse(self.handler.touched)

    def test_requested_edge_key_does_not_read_other_engine_cache(self):
        body = self.request()
        self.put(body, engine="gtts")
        self.serve(body)
        self.assertEqual(self.handler.status, 409)

    def test_non_mp3_cache_is_not_used_for_mp3_stream(self):
        body = self.request()
        self.put(body, fmt="wav")
        self.serve(body)
        self.assertEqual(self.handler.status, 409)

    def test_empty_or_oversized_cache_is_rejected_without_provider(self):
        for data in (b"", b"a" * 65537):
            with self.subTest(size=len(data)):
                self.handler = Handler(self.root)
                self.starts = 0
                self.done.clear()
                self.json_responses.clear()
                body = self.request()
                self.put(body, data)
                self.serve(body)
                self.assertEqual(self.handler.status, 409)

    def test_locked_cache_is_a_miss_not_a_synthesis_request(self):
        import fcntl
        body = self.request()
        path = self.put(body)
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.serve(body)
        self.assertEqual(self.handler.status, 409)

    def test_file_truncated_after_check_never_finishes_successful_stream(self):
        body = self.request()
        path = self.put(body, b"complete-audio" * 3000)
        self.handler._touch_tts_cache_file = lambda _: path.write_bytes(b"truncated")
        self.serve(body)
        self.assertEqual(self.handler.status, 200)
        self.assertFalse(self.handler.wfile.getvalue().endswith(b"0\r\n\r\n"))
        self.assertFalse(self.done[0]["ok"])
        self.assertIn("cache incompleto", self.done[0]["error"])

    def test_file_truncated_to_empty_before_headers_returns_conflict(self):
        body = self.request()
        path = self.put(body)
        self.handler._touch_tts_cache_file = lambda _: path.write_bytes(b"")
        self.serve(body)
        self.assertEqual(self.handler.status, 409)
        self.assertEqual(self.json_responses[0][1]["error"], "cache_incomplete")
        self.assertFalse(self.handler.wfile.getvalue())

    def test_normal_miss_still_uses_provider_and_publishes_complete_cache(self):
        body = self.request(cache_mode="prefer")
        source = mock.Mock()
        source.__iter__ = mock.Mock(return_value=iter((b"new-audio", b"tail")))
        with mock.patch.object(transport, "AudioStream", return_value=source) as provider:
            transport.serve_stream(self.handler, body, self.api)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.kwargs["engine"], "edge")
        self.assertEqual(self.handler.headers["X-Core-Worker-Cache-Hit"], "false")
        key = self.handler._tts_agent_standard_cache_key(body, engine="edge")
        self.assertEqual((self.root / (key + ".mp3")).read_bytes(), b"new-audiotail")
        self.assertTrue(self.done[0]["ok"])

    def test_cache_only_modes_are_read_only_in_existing_policy(self):
        for mode in ("only", "cache_only"):
            self.assertTrue(policy.cache_mode_allows_read({"cache_mode": mode}))
            self.assertFalse(policy.cache_mode_allows_store({"cache_mode": mode}))


if __name__ == "__main__":
    unittest.main()
