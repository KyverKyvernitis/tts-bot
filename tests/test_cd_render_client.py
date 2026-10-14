"""Authenticated local-worker integration using a real loopback HTTP server."""
from pathlib import Path
import ast
from email.message import Message
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/ttv-cd"))
import worker_config
sys.path.insert(0, str(ROOT / "tests"))
from test_cd_multivoice_render import tone, arrangement


class WorkerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requested.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer local-canary-secret":
            self.send_response(401); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"version": "1.11.41", "tts_agent": {
            "synth_ready": True, "active": self.server.active,
            "teto": {"ttv_voices": [{"id": "kasane-teto", "singing_fingerprints": {
                "pt-BR": "a" * 64}}]}}}).encode())

    def do_POST(self):
        self.server.requested.append((self.path, self.headers.get("Authorization")))
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(request)
        audio = tone(request["ttv_score"], 440 if request["ttv_score"]["notes"][0]["midi_note"] == 69 else 660, 125)
        self.send_response(200)
        for key, value in {"Content-Type": "audio/wav", "Content-Length": str(len(audio)),
            "X-Core-Worker-Audio-Format": "wav", "X-Core-Worker-Version": "1.11.41",
            "X-Core-Worker-Teto-Renderer": "speech-4g-cvvc-pauses",
            "X-Core-Worker-Teto-Timeline": "midi-vowel-continuous",
            "X-Core-Worker-Sha256": hashlib.sha256(audio).hexdigest()}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(audio)


class LocalRenderClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cd-local-http-")
        self.root = Path(self.temporary.name)
        self.server = None
        if self._testMethodName in {
                "test_configuration_fingerprint_tracks_recording_changes_without_disclosing_paths",
                "test_credentials_are_only_loaded_into_explicit_private_transport_snapshot"}:
            return
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), WorkerHandler)
        except PermissionError:
            self.temporary.cleanup()
            self.skipTest("sandbox forbids loopback TCP; HTTP integration requires network permission")
        self.server.active = 0
        self.server.requested = []
        self.server.payloads = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = {"PHONE_WORKER_PORT": str(self.server.server_port),
                            "PHONE_WORKER_TOKEN": "local-canary-secret"}

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temporary.cleanup()

    def test_actual_authenticated_status_and_raw_http_keep_v1_protocol(self):
        client = worker_config.LocalWorkerClient(self.environment)
        client.status()
        self.assertEqual(len(client.render_profile_fingerprint()), 64)
        score = {"version": 1, "track_index": 0, "duration_ms": 1000,
                 "lyrics_source": "message", "notes": arrangement()["parts"][0]["notes"]}
        audio, metadata = client.render_part(score=score, text="la")
        self.assertGreater(len(audio), 44)
        self.assertEqual(metadata["worker_version"], "1.11.41")
        self.assertEqual(self.server.payloads[0]["ttv_score"], score)
        self.assertTrue(all(auth == "Bearer local-canary-secret" for _, auth in self.server.requested))

    def test_worker_busy_never_bypasses_admission_with_an_isolated_renderer(self):
        self.server.active = 1
        target = self.root / "existing.mp3"
        target.write_bytes(b"old-completed-artifact")
        with patch.object(worker_config, "load_environment", return_value=self.environment), \
             patch.object(worker_config, "find_release", return_value=ROOT / "deploy/termux/phone-worker"), \
             patch.object(worker_config, "resource_ready", return_value=True), \
             patch.object(worker_config, "_isolated_adapter") as isolated:
            result = worker_config.render_sample({"duration_ms": 1000, "notes": []}, target,
                                                  allow_isolated=True)
        self.assertEqual(result["status"], "pending_worker_busy")
        self.assertEqual(target.read_bytes(), b"old-completed-artifact")
        isolated.assert_not_called()
        self.assertNotIn("local-canary-secret", json.dumps(result))

    def test_v2_sample_uses_active_worker_produces_decodable_atomic_mp3(self):
        target = self.root / "sample.mp3"
        with patch.object(worker_config, "load_environment", return_value=self.environment), \
             patch.object(worker_config, "find_release", return_value=ROOT / "deploy/termux/phone-worker"), \
             patch.object(worker_config, "resource_ready", return_value=True):
            result = worker_config.render_sample({"arrangement": arrangement()}, target)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["sample_origin"], "active-worker-http")
        self.assertEqual(result["mp3_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(len(result["stems"]), 2)
        self.assertNotIn("local-canary-secret", json.dumps(result))
        self.assertEqual(len(self.server.payloads), 2)

    def test_configuration_fingerprint_tracks_recording_changes_without_disclosing_paths(self):
        bank = self.root / "private-bank"
        bank.mkdir()
        recording = bank / "a.wav"
        recording.write_bytes(b"recording-one")
        env = self.root / "worker.env"
        env.write_text("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR='" + str(bank) + "'\n")
        before = worker_config.configuration_fingerprint(env)
        recording.write_bytes(b"different-recording")
        after = worker_config.configuration_fingerprint(env)
        self.assertNotEqual(before, after)
        self.assertEqual(len(after), 64)

    def test_credentials_are_only_loaded_into_explicit_private_transport_snapshot(self):
        env = self.root / "worker.env"
        env.write_text("PHONE_WORKER_TOKEN=local-canary-secret\n")
        self.assertNotIn("PHONE_WORKER_TOKEN", worker_config.load_environment(env))
        self.assertEqual(worker_config.load_environment(env, include_worker_credentials=True)["PHONE_WORKER_TOKEN"],
                         "local-canary-secret")


class ProtocolResponse(io.BytesIO):
    def __init__(self, audio, headers):
        super().__init__(audio)
        self.headers = headers


class ProtocolHandler:
    """In-memory wire capture executing the unchanged worker's header writer."""
    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = Message()
    def send_response(self, code):
        self.code = code
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass


class TransportContractTests(unittest.TestCase):
    """No TCP: real worker response code plus a captured client transport.

    This checks wire compatibility and composition with native FFmpeg, without
    claiming a live HTTP connection or installed voicebank synthesis.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cd-render-protocol-")
        self.root = Path(self.temp.name)
        self.calls = []
        self.active = 0
        worker_source = ROOT / "deploy/termux/phone-worker/phone_worker.py"
        if not worker_source.is_file():
            self.skipTest("Actual worker HTTP header source is unavailable in this package")
        source = ast.parse(worker_source.read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_audio_response")
        namespace = {"BaseHTTPRequestHandler": object, "Any": object,
                     "_header_ascii": lambda value, **_: str(value or "")}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "real_worker_audio_response", "exec"), namespace)
        self.audio_response = namespace["_audio_response"]
        self.client = worker_config.LocalWorkerClient({"PHONE_WORKER_TOKEN": "local-canary-secret"})
        self.client._opener = self

    def tearDown(self):
        self.temp.cleanup()

    def open(self, request, timeout):
        self.assertEqual(request.get_header("Authorization"), "Bearer local-canary-secret")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:8766/"))
        self.calls.append(request)
        if request.data is None:
            return ProtocolResponse(json.dumps({"version": "1.11.41", "tts_agent": {
                "synth_ready": True, "active": self.active, "teto": {"ttv_voices": [
                    {"id": "kasane-teto", "singing_fingerprints": {"pt-BR": "a" * 64}}]}}}).encode(), Message())
        body = json.loads(request.data)
        raw = tone(body["ttv_score"], 440, 125)
        handler = ProtocolHandler()
        self.audio_response(handler, 200, raw, {"audio_format": "wav", "engine": "teto",
            "cache_hit": False, "worker_version": "1.11.41", "teto_renderer_version": "speech-4g-cvvc-pauses",
            "teto_timeline_mode": "midi-vowel-continuous", "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(handler.code, 200)
        return ProtocolResponse(handler.wfile.getvalue(), handler.headers)

    def test_real_header_writer_and_segments_match_existing_raw_client(self):
        score = {"version": 1, "track_index": 0, "duration_ms": 1000,
                 "lyrics_source": "message", "notes": arrangement()["parts"][0]["notes"]}
        segment = {"text": "la", "fade_in": True, "fade_out": False, "hold": False, "pause_after_ms": 0}
        raw, meta = self.client.render_part(score=score, text="la", segments=[segment])
        self.assertGreater(len(raw), 44)
        self.assertEqual(meta["worker_version"], "1.11.41")
        self.assertEqual(meta["teto_timeline_mode"], "midi-vowel-continuous")
        self.assertEqual(json.loads(self.calls[-1].data)["ttv_segments"], [segment])

    def test_busy_worker_leaves_existing_sample_and_never_instantiates_renderer(self):
        self.active = 1
        target = self.root / "old.mp3"
        target.write_bytes(b"existing-validated-audio")
        with patch.object(worker_config, "load_environment", return_value={}), \
             patch.object(worker_config, "configuration_fingerprint", return_value="a" * 64), \
             patch.object(worker_config, "find_release", return_value=ROOT / "deploy/termux/phone-worker"), \
             patch.object(worker_config, "resource_ready", return_value=True), \
             patch.object(worker_config, "LocalWorkerClient", return_value=self.client), \
             patch.object(worker_config, "_isolated_adapter") as isolated:
            result = worker_config.render_sample({"arrangement": arrangement()}, target, allow_isolated=True)
        self.assertEqual(result["status"], "pending_worker_busy")
        self.assertTrue(result["retryable"])
        self.assertEqual(target.read_bytes(), b"existing-validated-audio")
        isolated.assert_not_called()

    def test_unsupported_long_note_is_permanent_until_identity_or_manual_retry_changes(self):
        value = arrangement(20000)
        with patch.object(worker_config, "load_environment", return_value={}), \
             patch.object(worker_config, "configuration_fingerprint", return_value="a" * 64), \
             patch.object(worker_config, "find_release", return_value=ROOT / "deploy/termux/phone-worker"), \
             patch.object(worker_config, "resource_ready", return_value=True), \
             patch.object(worker_config, "LocalWorkerClient", return_value=self.client):
            result = worker_config.render_sample({"arrangement": value}, self.root / "sample.mp3")
        self.assertEqual(result["error_code"], "note_exceeds_vowel_limit")
        self.assertFalse(result["retryable"])
        self.assertEqual(len(self.calls), 1)  # status only; no physical render

    def test_sample_uses_protocol_writer_and_produces_atomic_decodable_mp3(self):
        target = self.root / "sample.mp3"
        with patch.object(worker_config, "load_environment", return_value={}), \
             patch.object(worker_config, "configuration_fingerprint", return_value="a" * 64), \
             patch.object(worker_config, "find_release", return_value=ROOT / "deploy/termux/phone-worker"), \
             patch.object(worker_config, "resource_ready", return_value=True), \
             patch.object(worker_config, "LocalWorkerClient", return_value=self.client):
            result = worker_config.render_sample({"arrangement": arrangement()}, target)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["mp3_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(len(result["stems"]), 2)
        self.assertNotIn("local-canary-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
