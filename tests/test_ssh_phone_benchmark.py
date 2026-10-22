"""Benchmark requests remain authenticated, cache-only, bounded and comparable."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

SOURCE = Path(__file__).resolve().parents[1] / "deploy/oci/benchmark_phone_ssh.py"
SPEC = importlib.util.spec_from_file_location("ssh_phone_benchmark_tests", SOURCE)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def options(**changes):
    values = dict(ssh_base="http://127.0.0.1:18766", baseline_base="http://100.70.0.2:8766",
                  total_seconds=180, request_timeout=5, samples=2, audio_text="",
                  voice="pt-BR-FranciscaNeural", cache_key="", warm_audio=False)
    values.update(changes)
    return argparse.Namespace(**values)


class Response:
    status = 200

    def __init__(self, body=b"", headers=None, fail_after_first=False):
        self.body = io.BytesIO(body)
        self.headers = headers or {}
        self.fail_after_first = fail_after_first
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, count):
        self.reads += 1
        if self.fail_after_first and self.reads > 1:
            raise OSError("stream truncado")
        return self.body.read(count)

    read1 = read


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


def audio_headers(**changes):
    values = {"X-Core-Worker-Id": "phone-1", "X-Core-Worker-Engine": "edge",
              "X-Core-Worker-Cache-Hit": "true", "X-Core-Worker-Stream-Protocol": "2"}
    values.update(changes)
    return values


def spec(**changes):
    values = {"kind": "health", "base": "http://127.0.0.1:18766", "auth": "secret-bearer",
              "worker_id": "phone-1", "timeout": 5, "text": "Teste já cacheado.", "cache_key": "a" * 64}
    values.update(changes)
    return values


def healthy_result(**changes):
    values = {"ok": True, "cache_only_protocol": 1, "total_ms": 100, "first_audio_ms": 80,
              "sha256": "a" * 64, "cache_hit": True, "pid": 123, "process_started_at": 1000,
              "version": "1.11.42", "transport_generation": 1}
    values.update(changes)
    return values


class ProbeSafety(unittest.TestCase):
    def test_health_verifies_identity_and_edge_readiness(self):
        for wrong in ({"worker_id": "other"}, {"ok": False}, {"tts_agent": {"ok": False}}):
            body = {"ok": True, "worker_id": "phone-1", "tts_agent": {"ok": True, "available_engines": ["edge"]}}
            body.update(wrong)
            result = bench.probe(spec(), opener=Opener(Response(json.dumps(body).encode())))
            self.assertFalse(result["ok"])
        body = {"ok": True, "worker_id": "phone-1", "version": "1.11.42", "tts_agent": {
            "ok": True, "available_engines": ["teto", "edge"], "stream_cache_only_protocol": 1, "stream_protocol": 2}}
        opener = Opener(Response(json.dumps(body).encode()))
        result = bench.probe(spec(), opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cache_only_protocol"], 1)
        request, _ = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-bearer")
        self.assertEqual(request.get_method(), "GET")

    def test_cached_audio_uses_only_mode_and_validates_full_bytes(self):
        opener = Opener(Response(b"mp3-cache-content", audio_headers()))
        result = bench.probe(spec(kind="audio"), opener=opener)
        body = json.loads(opener.requests[0][0].data)
        self.assertEqual(body["cache_mode"], "only")
        self.assertEqual(body["engine"], body["fallback_engine"])
        self.assertEqual(body["engine"], "edge")
        self.assertEqual(body["cache_key"], "a" * 64)
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio_bytes"], len(b"mp3-cache-content"))

    def test_uncached_or_wrong_engine_is_rejected_before_reading_audio(self):
        for headers in (audio_headers(**{"X-Core-Worker-Cache-Hit": "false"}),
                        audio_headers(**{"X-Core-Worker-Engine": "gtts"}),
                        audio_headers(**{"X-Core-Worker-Id": "wrong"}),
                        audio_headers(**{"X-Core-Worker-Stream-Protocol": "1"})):
            response = Response(b"unexpected", headers)
            result = bench.probe(spec(kind="audio"), opener=Opener(response))
            self.assertFalse(result["ok"])
            self.assertEqual(response.reads, 0)

    def test_truncated_stream_cannot_be_accepted_as_complete(self):
        response = Response(b"incomplete", audio_headers(), fail_after_first=True)
        result = bench.probe(spec(kind="audio"), opener=Opener(response))
        self.assertFalse(result["ok"])
        self.assertNotIn("sha256", result)

    def test_warm_request_is_explicit_and_edge_only(self):
        opener = Opener(Response(b"seed", audio_headers(**{"X-Core-Worker-Cache-Hit": "false"})))
        result = bench.probe(spec(kind="warm"), opener=opener)
        self.assertTrue(result["ok"])
        body = json.loads(opener.requests[0][0].data)
        self.assertEqual(body["cache_mode"], "prefer")
        self.assertEqual(body["preferred_engine"], body["fallback_engine"])

    def test_redirects_never_replay_authorization(self):
        result = bench.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://other/")
        self.assertIsNone(result)


class BenchmarkSafety(unittest.TestCase):
    def test_default_health_only_does_not_request_synthesis(self):
        calls = []
        result = bench.benchmark(options(), "private", "phone-1", requester=lambda request:
                                 calls.append(request) or healthy_result())
        self.assertTrue(result["complete_health"])
        self.assertFalse(result["complete_audio"])
        self.assertFalse(result["migration_speed_criterion_passed"])
        self.assertEqual({call["kind"] for call in calls}, {"health"})
        self.assertEqual([call["base"] for call in calls[2:]], [
            "http://127.0.0.1:18766", "http://100.70.0.2:8766",
            "http://100.70.0.2:8766", "http://127.0.0.1:18766"])

    def test_missing_capability_skips_audio_even_when_warming_requested(self):
        calls = []
        result = bench.benchmark(options(audio_text="Cached", warm_audio=True), "private", "phone-1",
                                 requester=lambda request: calls.append(request) or healthy_result(cache_only_protocol=0))
        self.assertFalse(result["complete_audio"])
        self.assertIsNone(result["warm_request"])
        self.assertEqual({call["kind"] for call in calls}, {"health"})

    def test_only_one_explicit_warm_and_cached_audio_comparison(self):
        calls = []
        def request(row):
            calls.append(row)
            fast = row["base"].startswith("http://127.")
            return healthy_result(total_ms=80 if fast else 100, first_audio_ms=64 if fast else 80)
        result = bench.benchmark(options(audio_text="Cached", warm_audio=True), "private", "phone-1", requester=request)
        self.assertEqual(sum(row["kind"] == "warm" for row in calls), 1)
        self.assertEqual(sum(row["kind"] == "audio" for row in calls), 4)
        self.assertTrue(result["complete_audio"])
        self.assertTrue(result["migration_speed_criterion_passed"])
        self.assertEqual(result["comparison"]["first_audio"]["ssh_gain_percent"], 20)

    def test_cache_miss_stops_audio_without_replacement_seed(self):
        calls = []
        def request(row):
            calls.append(row)
            return {"ok": False, "http_status": 404} if row["kind"] == "audio" else healthy_result()
        result = bench.benchmark(options(audio_text="Absent"), "private", "phone-1", requester=request)
        self.assertEqual(sum(row["kind"] == "audio" for row in calls), 1)
        self.assertEqual(sum(row["kind"] == "warm" for row in calls), 0)
        self.assertFalse(result["comparison"]["first_audio"]["comparable"])

    def test_failed_preflight_stops_before_sampling_or_warming(self):
        calls = []
        result = bench.benchmark(options(audio_text="text", warm_audio=True), "private", "phone-1",
                                 requester=lambda row: calls.append(row) or {"ok": False})
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["complete_health"])

    def test_different_runtime_between_routes_is_rejected(self):
        calls = []
        def request(row):
            calls.append(row)
            return healthy_result(pid=1 if row["base"].startswith("http://127.") else 2)
        result = bench.benchmark(options(audio_text="Cached", warm_audio=True), "private", "phone-1", requester=request)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["complete_health"])
        self.assertIsNone(result["warm_request"])

    def test_runtime_restart_during_sampling_invalidates_comparison(self):
        calls = []
        def request(row):
            calls.append(row)
            return healthy_result(pid=123 if len(calls) <= 2 else 456)
        result = bench.benchmark(options(audio_text="Cached"), "private", "phone-1", requester=request)
        self.assertFalse(result["complete_health"])
        self.assertFalse(result["comparison"]["health"]["comparable"])
        self.assertEqual(sum(row["kind"] == "audio" for row in calls), 0)

    def test_changed_audio_invalidates_comparison(self):
        calls = []
        def request(row):
            calls.append(row)
            return healthy_result(sha256="a" * 64 if row["base"].startswith("http://127.") else "b" * 64)
        result = bench.benchmark(options(audio_text="Cached"), "private", "phone-1", requester=request)
        self.assertFalse(result["complete_audio"])
        self.assertFalse(result["comparison"]["first_audio"]["comparable"])

    def test_regression_assessment_does_not_claim_speed_gain(self):
        def request(row):
            slow = row["base"].startswith("http://127.")
            return healthy_result(total_ms=110 if slow else 100, first_audio_ms=88 if slow else 80)
        result = bench.benchmark(options(audio_text="Cached"), "private", "phone-1", requester=request)
        self.assertFalse(result["migration_speed_criterion_passed"])
        self.assertFalse(result["comparison"]["health"]["no_regression_over_5_percent"])
        self.assertEqual(result["comparison"]["health"]["ssh_gain_percent"], -10)

    def test_single_route_is_supported_but_cannot_prove_improvement(self):
        result = bench.benchmark(options(baseline_base=None, audio_text="Cached"), "private", "phone-1",
                                 requester=lambda row: healthy_result())
        self.assertTrue(result["complete_health"])
        self.assertTrue(result["complete_audio"])
        self.assertFalse(result["comparison"]["first_audio"]["comparable"])
        self.assertFalse(result["migration_speed_criterion_passed"])


class InputsAndReports(unittest.TestCase):
    def test_url_credentials_redirect_shapes_and_public_http_are_rejected(self):
        for value in ("http://user:secret@127.0.0.1:18766", "http://127.0.0.1:18766/path",
                      "http://127.0.0.1:18766?x=1", "http://127.0.0.1:18766#secret",
                      "file:///tmp/local", "http://8.8.8.8:8766"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bench.validate_base(value)
        self.assertEqual(bench.validate_base("http://100.103.240.118:8766"), "http://100.103.240.118:8766")
        with self.assertRaises(ValueError):
            bench.validate_base("https://phone.example", ssh=True)

    def test_nested_report_redaction_preserves_valid_json(self):
        original = {"body": {"token": "x", "message": 'aspas " barra \\ SENTINEL-KEY'}, "error": "Bearer OTHER"}
        result = bench.clean(original, ["SENTINEL-KEY"])
        text = json.dumps(result)
        self.assertNotIn("SENTINEL-KEY", text)
        self.assertNotIn("OTHER", text)
        self.assertEqual(json.loads(text)["body"]["message"], 'aspas " barra \\ [OCULTO]')

    def test_outputs_are_private_and_text_marks_partial(self):
        result = bench.benchmark(options(baseline_base=None), "private", "phone-1",
                                 requester=lambda row: healthy_result())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.txt"
            bench.atomic_output(path, bench.text_report(result))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("diagnóstico parcial", path.read_text())
            self.assertIn("sem comparação válida", path.read_text())

    def test_nearest_rank_percentiles(self):
        self.assertEqual(bench.percentile(range(1, 51), .95), 48)
        self.assertEqual(bench.percentile(range(1, 51), .5), 25)
        self.assertIsNone(bench.percentile([], .95))


if __name__ == "__main__":
    unittest.main()
