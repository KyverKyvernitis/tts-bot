"""Authenticated remote bridge, exercised with a fake desktop executable."""
from __future__ import annotations

import base64
import http.client
import importlib.util
import io
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

from test_voicepeak_backend import VoicepeakRenderer, voicepeak_assets

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("test_voicepeak_host_server", ROOT / "deploy" / "voicepeak-teto" / "server.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


@pytest.fixture
def live_bridge(voicepeak_assets):
    renderer = VoicepeakRenderer(allow_remote=False)
    server = bridge.create_server("127.0.0.1", 0, renderer=renderer, token="test-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(url, path, body=None, token="test-secret", headers=None):
    data = None if body is None else json.dumps(body).encode()
    request_headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url + path, data=data, headers=request_headers)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def test_remote_to_local_fake_cli_end_to_end(live_bridge, voicepeak_assets, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TEXT_MODE", "ptbr-kana")
    # Low resources on the phone do not block remote CPU work.
    remote = VoicepeakRenderer(resource_guard=lambda: {"ok": False, "reason": "phone has little free RAM"})
    status = remote.status()
    assert status["ready"] and status["source"] == "remote"
    assert status["host_fingerprint"] == server.renderer.fingerprint()
    result = remote.synthesize("Olá, minha filha!", timeout_seconds=5)
    assert result["audio"].startswith(b"RIFF") and result["source"] == "remote"
    assert result["reading_text"] == "おら, みにゃ ふぃりゃ!"
    assert result["renderer_fingerprint"] == status["fingerprint"]
    assert result["renderer_fingerprint"] != status["host_fingerprint"]
    assert "test-secret" not in json.dumps(status)
    with wave.open(io.BytesIO(result["audio"])) as audio:
        assert audio.getnframes() > 0
    assert server.renderer.status()["source"] == "native"  # Never forwards recursively.


@pytest.mark.parametrize("token", ("", "wrong-secret"))
def test_authentication_applies_to_status_and_synthesis(live_bridge, token, voicepeak_assets):
    _, url = live_bridge
    _, _, calls = voicepeak_assets
    assert request(url, "/status", token=token)[0] == 401
    assert request(url, "/synthesize", {"text": "こんにちは"}, token=token)[0] == 401
    assert not calls()


def test_server_requires_token_and_refuses_client_voice_or_command_control(voicepeak_assets, live_bridge):
    with pytest.raises(ValueError, match="TOKEN"):
        bridge.create_server("127.0.0.1", 0, token="")
    _, url = live_bridge
    for field in ("command", "narrator", "reading_mode", "speed", "out"):
        assert request(url, "/synthesize", {"text": "こんにちは", field: "bad"})[0] == 400


@pytest.mark.parametrize("body", ({"text": ""}, {"text": 1}, {"text": "こんにちは", "timeout_seconds": float("nan")}, {"text": "こんにちは", "timeout_seconds": float("inf")}, {"text": "こんにちは", "max_audio_bytes": 9 * 1024 * 1024}, {"text": "こんにちは", "max_audio_bytes": True}))
def test_bridge_request_validation(live_bridge, body):
    _, url = live_bridge
    assert request(url, "/synthesize", body)[0] == 400


def test_bridge_rejects_oversized_body_before_reading(live_bridge):
    server, _ = live_bridge
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.putrequest("POST", "/synthesize")
    connection.putheader("Authorization", "Bearer test-secret")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(bridge.MAX_REQUEST_BYTES + 1))
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    response.read()
    connection.close()


def test_bridge_busy_response_preserves_single_engine_admission(live_bridge):
    server, url = live_bridge
    with server.renderer._lock:
        assert request(url, "/synthesize", {"text": "こんにちは"})[0] == 409


def test_bridge_failed_engine_and_timeout_are_explicit(live_bridge, voicepeak_assets):
    _, url = live_bridge
    _, configure, _ = voicepeak_assets
    configure(narrators=["別の声"])
    assert request(url, "/synthesize", {"text": "こんにちは"})[0] == 503
    configure(narrators=["重音テト"], delay=0.2)
    # Recheck after the intentionally failed status cached above.
    live_bridge[0].renderer._last_status = {}
    code, _ = request(url, "/synthesize", {"text": "こんにちは", "timeout_seconds": 0.25})
    assert code in {503, 504}


def test_remote_missing_token_and_wrong_token_are_not_ready(live_bridge, monkeypatch):
    _, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    status = VoicepeakRenderer().status()
    assert not status["ready"] and "TOKEN" in status["last_error"]
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "wrong")
    status = VoicepeakRenderer().status()
    assert not status["ready"] and "autenticação" in status["last_error"]


def test_remote_reading_mode_and_narrator_must_match_host(live_bridge, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    original = server.renderer.status

    def mismatched_mode(**kwargs):
        status = original(**kwargs)
        status["reading_mode"] = "ptbr-kana"
        return status

    monkeypatch.setattr(server.renderer, "status", mismatched_mode)
    assert "TEXT_MODE" in VoicepeakRenderer().status()["last_error"]

    def mismatched_narrator(**kwargs):
        status = original(**kwargs)
        status["narrator"] = "別の声"
        return status

    monkeypatch.setattr(server.renderer, "status", mismatched_narrator)
    assert "narrador" in VoicepeakRenderer().status()["last_error"]


def test_remote_changed_engine_between_status_and_synthesis_is_rejected(live_bridge, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    remote = VoicepeakRenderer()
    assert remote.status()["ready"]
    original = server.renderer.synthesize

    def changed_engine(*args, **kwargs):
        result = original(*args, **kwargs)
        result["renderer_fingerprint"] = "different-host-engine"
        return result

    monkeypatch.setattr(server.renderer, "synthesize", changed_engine)
    with pytest.raises(RuntimeError, match="configuração VOICEPEAK mudou"):
        remote.synthesize("こんにちは")
    assert not remote._last_status


def test_remote_validates_audio_and_configured_size_limit(live_bridge, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    remote = VoicepeakRenderer()
    assert remote.status()["ready"]
    original = remote._request

    def broken_audio(config, method, path, deadline, body=None, max_bytes=None):
        result = original(config, method, path, deadline, body=body, max_bytes=max_bytes)
        if method == "POST":
            result["audio_base64"] = base64.b64encode(b"broken wav").decode()
        return result

    monkeypatch.setattr(remote, "_request", broken_audio)
    with pytest.raises(RuntimeError, match="WAV PCM"):
        remote.synthesize("こんにちは")


def test_bearer_token_is_not_forwarded_to_redirect_target(live_bridge, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    captured = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(self.path)
            if self.path == "/status":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", "http://127.0.0.1:" + str(redirect.server_port))
        status = VoicepeakRenderer().status()
        assert not status["ready"] and "302" in status["last_error"]
        assert captured == ["/status"]
    finally:
        redirect.shutdown()
        redirect.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("response_kind", ("oversized-header", "oversized-body", "trickle"))
def test_http_response_is_bounded_by_size_and_total_deadline(voicepeak_assets, monkeypatch, response_kind):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Response(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if response_kind == "oversized-header":
                self.send_header("Content-Length", "65537")
                self.end_headers()
                return
            self.end_headers()
            try:
                if response_kind == "oversized-body":
                    self.wfile.write(b"x" * 65537)
                    self.wfile.flush()
                else:
                    # Individual socket progress must not reset a total deadline.
                    for char in b'{"ready":true,"backend":"voicepeak"}':
                        self.wfile.write(bytes([char]))
                        self.wfile.flush()
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Response)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", "http://127.0.0.1:" + str(server.server_port))
        monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
        renderer = VoicepeakRenderer()
        started = time.monotonic()
        status = renderer._status(deadline=started + 0.25)
        assert not status["ready"]
        if response_kind == "trickle":
            assert time.monotonic() - started < 0.8
            assert "prazo" in status["last_error"]
        else:
            assert "grande demais" in status["last_error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_fingerprint_tracks_phone_caps_and_excludes_token(live_bridge, monkeypatch):
    server, url = live_bridge
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_URL", url)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "test-secret")
    host_status = server.renderer.status()
    # Fix the host configuration; only the client's caps/token change here.
    monkeypatch.setattr(server.renderer, "status", lambda **kwargs: dict(host_status))
    remote = VoicepeakRenderer()
    first = remote.fingerprint()
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", "1")
    second = remote.fingerprint()
    assert first != second
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_CHARACTERS", "100")
    third = remote.fingerprint()
    assert third != second
    server.token = "changed-secret"
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TOKEN", "changed-secret")
    assert remote.fingerprint() == third


def test_standalone_script_starts_in_isolated_python_from_external_cwd(voicepeak_assets, tmp_path, monkeypatch):
    import os
    import queue
    import subprocess
    import sys

    env = dict(os.environ)
    env.update(VOICEPEAK_TOKEN="standalone-secret", VOICEPEAK_HOST="127.0.0.1", VOICEPEAK_PORT="0",
               PHONE_WORKER_VOICEPEAK_TEXT_MODE="ptbr-kana", PYTHONPATH="/unrelated-python-path")
    # The process has none of pytest's sys.path/module changes. -I also ignores
    # PYTHONPATH and excludes the script directory/current directory by default.
    process = subprocess.Popen([sys.executable, "-I", "-u", str(ROOT / "deploy" / "voicepeak-teto" / "server.py")],
                               cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    startup = queue.Queue()
    reader = threading.Thread(target=lambda: startup.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = startup.get(timeout=5)
        assert "VOICEPEAK Teto bridge em 127.0.0.1:" in line, line or process.stderr.read()
        assert "; leitura=ptbr-kana" in line
        port = int(line.split("127.0.0.1:", 1)[1].split(";", 1)[0])
        url = "http://127.0.0.1:" + str(port)
        code, status = request(url, "/status", token="standalone-secret")
        assert code == 200 and status["ready"] and status["source"] == "native"
        code, rendered = request(url, "/synthesize", {"text": "Olá, Teto!"}, token="standalone-secret")
        assert code == 200 and rendered["metadata"]["reading_text"] == "おら, てとぅ!"
        assert base64.b64decode(rendered["audio_base64"]).startswith(b"RIFF")
    finally:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
        reader.join(timeout=1)
