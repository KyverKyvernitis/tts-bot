"""Native adapter contracts against local HTTP and controlled response boundaries."""
import base64
import binascii
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


@pytest.fixture
def native(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_android_tts_raw_request", tts.native_raw_request)
    monkeypatch.setattr(tts.worker, "_android_tts_json_request", tts.native_json_request)
    tts.deps.clear()
    tts.deps["android_native_tts"] = True
    return tts


@pytest.fixture
def local_apk(native, monkeypatch):
    replies, calls = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, dict(self.headers), payload))
            status, headers, data = replies.pop(0) if replies else (500, {}, b"unexpected request")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = respond
        do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_URL", f"http://127.0.0.1:{server.server_port}/")
    try:
        yield SimpleNamespace(replies=replies, calls=calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


class Response(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        self.headers = headers
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def transport(native, monkeypatch, body, headers=None):
    response = Response(body, headers or {})
    calls = []

    def open_url(request, **kwargs):
        calls.append((request, kwargs))
        return response

    monkeypatch.setattr(native.worker, "urllib", SimpleNamespace(request=SimpleNamespace(
        Request=urllib.request.Request, urlopen=open_url)))
    return response, calls


def synthesize(native, **settings):
    return native.handler._task_tts_agent_synthesize({"engine": "android_native", "text": "Olá, ação!",
        "cache_mode": "off", "timeout_seconds": 4, **settings})


def test_json_get_and_unicode_post_over_local_http(native, local_apk):
    local_apk.replies.extend([(200, {"Content-Type": "application/json"}, b'{"ready":true}'),
        (200, {"Content-Type": "application/json"}, b'{"voices":[]}')])
    assert native.worker._android_tts_json_request("/native-tts/status") == {"ready": True}
    payload = {"locale": "pt-BR", "text": "Ação!"}
    assert native.worker._android_tts_json_request("/native-tts/voices", payload=payload) == {"voices": []}
    get, post = local_apk.calls
    assert get[0:2] == ("GET", "/native-tts/status") and get[3] == b""
    assert get[2]["Accept"] == "application/json"
    assert post[0:2] == ("POST", "/native-tts/voices")
    assert post[2]["Content-Type"] == "application/json; charset=utf-8"
    assert post[2]["User-Agent"] == "CorePhoneWorker/" + native.worker.PHONE_WORKER_VERSION
    assert post[3] == json.dumps(payload, ensure_ascii=False).encode()


def test_raw_audio_and_headers_over_local_http(native, local_apk):
    raw = b"RIFF\x00binary-audio"
    headers = {"Content-Type": "Audio/WAV", "X-Core-Worker-Audio-Format": " WAV ",
        "X-Core-Worker-Android-Synth-Ms": " 12.3 ", "X-Core-Worker-Locale": " pt-BR ",
        "X-Core-Worker-Voice": " fixture ", "X-Core-Worker-Sha256": hashlib.sha256(raw).hexdigest()}
    local_apk.replies.append((200, headers, raw))
    data, meta = native.worker._android_tts_raw_request("/native-tts/synthesize.raw", payload={"text": "Ação"})
    assert data == raw
    assert meta == {"content_type": "audio/wav", "audio_format": "wav", "android_synth_ms": "12.3",
        "locale": "pt-BR", "voice": "fixture", "sha256": hashlib.sha256(raw).hexdigest()}
    request = local_apk.calls[0]
    assert request[0:2] == ("POST", "/native-tts/synthesize.raw")
    assert request[2]["Accept"] == "audio/wav,application/octet-stream,application/json;q=0.4,*/*;q=0.1"
    assert json.loads(request[3]) == {"text": "Ação"}


def test_local_http_raw_error_falls_back_once_to_json_without_changing_engine(native, local_apk):
    raw = b"native-json-audio"
    local_apk.replies.extend([(200, {"Content-Type": "application/json"}, b'{"ok":false,"error":"raw off"}'),
        (200, {"Content-Type": "application/json"}, json.dumps({"ok": True, "data_b64": base64.b64encode(raw).decode(),
         "audio_format": "wav", "voice": "fixture", "locale": "pt-BR", "android_synth_ms": 7}).encode())])
    result = synthesize(native)
    assert result["engine"] == result["selected_engine"] == "android_native" and audio(result) == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert [request[1] for request in local_apk.calls] == ["/native-tts/synthesize.raw", "/native-tts/synthesize"]
    assert local_apk.calls[0][3] == local_apk.calls[1][3]
    assert any("fallback json: RuntimeError: raw off" in line for line in result["logs"])
    assert not native.executor.jobs and native.worker._TTS_AGENT_ACTIVE == 0


@pytest.mark.parametrize("body,content_type,match", [
    (b"", "audio/wav", "não retornou áudio"),
    (b"a" * 1025, "audio/wav", "grande demais"),
    (b'broken', "application/json", "JSON inválido"),
    (b'{"ok":false,"error":"engine offline"}', "application/json", "engine offline"),
    (b'{"ok":false}', "application/json", "ok=false"),
    (b'{}', "application/json", "JSON sem áudio"),
    (b'[]', "application/json", "JSON sem áudio"),
])
def test_raw_errors_close_response_and_read_at_most_limit_plus_one(native, monkeypatch, body, content_type, match):
    response, calls = transport(native, monkeypatch, body, {"Content-Type": content_type})
    with pytest.raises(RuntimeError, match=match):
        native.worker._android_tts_raw_request("/raw", payload={}, max_audio_bytes=1024, timeout=0)
    assert response.closed and response.read_sizes == [1025]
    assert calls[0][1]["timeout"] == 0.1


@pytest.mark.parametrize("limit", [4, 1024, 4096])
def test_raw_accepts_exact_limit_and_keeps_default_metadata(native, monkeypatch, limit):
    data = b"a" * limit
    response, _ = transport(native, monkeypatch, data)
    received, meta = native.worker._android_tts_raw_request("/raw", payload={}, max_audio_bytes=limit)
    assert received == data and meta == {"content_type": "", "audio_format": "wav", "android_synth_ms": "",
        "locale": "", "voice": "", "sha256": ""}
    assert response.closed and response.read_sizes == [max(1024, limit) + 1]


@pytest.mark.parametrize("body,expected", [(b"", {}), (b'{"voice":"\xff"}', {"voice": "\ufffd"})])
def test_json_empty_and_utf8_replacement_behavior(native, monkeypatch, body, expected):
    response, calls = transport(native, monkeypatch, body)
    assert native.worker._android_tts_json_request("/status", timeout=0) == expected
    assert response.closed and response.read_sizes == [16 * 1024 * 1024]
    assert calls[0][1]["timeout"] == 0.1


@pytest.mark.parametrize("body,error", [(b"[]", RuntimeError), (b"1", RuntimeError), (b"null", RuntimeError), (b"invalid", ValueError)])
def test_json_rejects_nonobject_or_malformed_body_and_closes(native, monkeypatch, body, error):
    response, _ = transport(native, monkeypatch, body)
    with pytest.raises(error):
        native.worker._android_tts_json_request("/status")
    assert response.closed


@pytest.mark.parametrize("kind", ["raw", "json"])
@pytest.mark.parametrize("error", [urllib.error.URLError("controlled"), TimeoutError("controlled")])
def test_network_errors_propagate_at_request_boundary(native, monkeypatch, kind, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(native.worker, "urllib", SimpleNamespace(request=SimpleNamespace(
        Request=urllib.request.Request, urlopen=fail)))
    call = native.worker._android_tts_raw_request if kind == "raw" else native.worker._android_tts_json_request
    with pytest.raises(type(error)):
        call("/failure", payload={})


@pytest.mark.parametrize("mode", ["raw", "fallback", "json_only"])
@pytest.mark.parametrize("raw_response", [False, True])
def test_synthesis_preserves_payload_timeouts_metadata_and_audio(native, monkeypatch, mode, raw_response):
    worker = native.worker
    calls, clock = [], [100.0]
    worker.time.monotonic = lambda: clock[0]
    monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_PREFER_LOCAL_VOICE", "0")
    monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_RAW_ENABLED", "0" if mode == "json_only" else "1")
    data = b"native-bytes"

    def raw(path, **kwargs):
        calls.append((path, copy.deepcopy(kwargs)))
        clock[0] += 0.2
        if mode == "fallback":
            raise TimeoutError("raw timed out")
        return data, {"audio_format": "wave", "voice": "used-voice", "locale": "pt-PT",
            "android_synth_ms": "12.25", "sha256": "untrusted-metadata"}

    def json_request(path, **kwargs):
        calls.append((path, copy.deepcopy(kwargs)))
        clock[0] += 0.3
        return {"ok": True, "data_b64": base64.b64encode(data).decode(), "audio_format": "wav",
            "voice": "used-voice", "locale": "pt-PT", "android_synth_ms": 12.25}

    monkeypatch.setattr(worker, "_android_tts_raw_request", raw)
    monkeypatch.setattr(worker, "_android_tts_json_request", json_request)
    body = {"engine": "android_native", "text": " Olá! ", "cache_mode": "off", "timeout_seconds": 4,
        "android_timeout_ms": 2500, "language": "pt-BR", "locale": "pt-PT", "voice": "requested",
        "rate": 1.2, "pitch": 0.9, "fallback_voice": "wrong", "fallback_language": "en-US"}
    before = copy.deepcopy(body)
    result = native.handler._task_tts_agent_synthesize(body, raw_response=raw_response)
    assert body == before and audio(result) == data and result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["android_voice"] == "used-voice" and result["android_locale"] == "pt-PT"
    assert result["timing_ms"]["android_synth"] == 12.25
    assert result["timing_ms"]["android_roundtrip"] == {"raw": 200.0, "fallback": 500.0, "json_only": 300.0}[mode]
    assert len(calls) == (2 if mode == "fallback" else 1)
    for path, kwargs in calls:
        assert kwargs["timeout"] == 3.5
        assert kwargs["payload"] == {"text": "Olá!", "language": "pt-BR", "locale": "pt-PT", "voice": "requested",
            "rate": "1.2", "pitch": "0.9", "timeout_ms": 2500, "max_audio_bytes": 4096, "prefer_local_voice": False}
        if path.endswith(".raw"):
            assert kwargs["max_audio_bytes"] == 4096
    assert not native.executor.jobs and worker._TTS_AGENT_ACTIVE == 0 and worker._TTS_AGENT_FAILED == 0


@pytest.mark.parametrize("value,expected", [(1, 1000), (90000, 4000), (None, 4000)])
def test_android_synth_timeout_clamps_to_existing_bounds(native, monkeypatch, value, expected):
    captured = []

    def raw(path, **kwargs):
        captured.append(kwargs)
        return b"native", {}

    monkeypatch.setattr(native.worker, "_android_tts_raw_request", raw)
    result = synthesize(native, android_timeout_ms=value)
    assert result["ok"] and captured[0]["payload"]["timeout_ms"] == expected
    assert captured[0]["timeout"] == expected / 1000 + 1


@pytest.mark.parametrize("response,error,match", [
    ({"ok": False, "error": "offline"}, RuntimeError, "offline"),
    ({"ok": False}, RuntimeError, "ok=false"),
    ({"data_b64": "!!!"}, binascii.Error, "base64"),
    ({"data_b64": ""}, RuntimeError, "não gerou áudio"),
    ({"data_b64": base64.b64encode(b"a" * 1025).decode()}, ValueError, "payload grande demais"),
])
def test_json_synthesis_errors_do_not_publish_cache(native, monkeypatch, response, error, match):
    monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_RAW_ENABLED", "0")
    monkeypatch.setattr(native.worker, "_android_tts_json_request", lambda *a, **kw: response)
    with pytest.raises(error, match=match):
        native.handler._synthesize_standard_tts_bytes({"text": "Erro", "cache_mode": "off"}, engine="android_native",
            roles=[], capabilities=[], logs=[], started=100, max_audio_bytes=1024, timeout=4)
    assert not native.executor.jobs and not native.root.exists()


def test_raw_and_json_failure_balance_admission_without_retrying_requests(native, monkeypatch):
    calls = []

    def fail(path, **kwargs):
        calls.append(path)
        raise TimeoutError("controlled native timeout")

    monkeypatch.setattr(native.worker, "_android_tts_raw_request", fail)
    monkeypatch.setattr(native.worker, "_android_tts_json_request", fail)
    with pytest.raises(RuntimeError, match="controlled native timeout"):
        synthesize(native)
    assert calls == ["/native-tts/synthesize.raw", "/native-tts/synthesize"]
    assert native.worker._TTS_AGENT_ACTIVE == 0 and native.worker._TTS_AGENT_TOTAL == native.worker._TTS_AGENT_FAILED == 1


def test_status_cache_expiry_force_refresh_and_error_preserve_one_owner(native, monkeypatch):
    calls = []
    clock = [100.0]
    native.worker.time.monotonic = lambda: clock[0]

    def status(path, **kwargs):
        calls.append((path, kwargs))
        if len(calls) == 3:
            raise TimeoutError("status unavailable")
        return {"ready": True, "voice": "fixture"}

    monkeypatch.setattr(native.worker, "_android_tts_json_request", status)
    first = native.worker._android_tts_status()
    first["voice"] = "mutated-copy"
    clock[0] = 105.0
    assert native.worker._android_tts_status()["voice"] == "fixture" and len(calls) == 1
    clock[0] = 105.001
    assert native.worker._android_tts_status()["ready"] and len(calls) == 2
    result = native.worker._android_tts_status(use_cache=False)
    assert not result["ready"] and "status unavailable" in result["last_error"] and len(calls) == 3
    assert native.worker._ANDROID_TTS_STATUS_CACHE["data"] == result


def test_disabled_android_skips_status_cache_and_voice_requests(native, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_ENABLED", "0")
    monkeypatch.setattr(native.worker, "_android_tts_json_request", lambda *a, **kw: pytest.fail("disabled Android requested IO"))
    assert not native.worker._android_tts_status()["enabled"]
    assert native.worker._android_tts_voices()["voices"] == []


@pytest.mark.parametrize("limit,clamped", [(-1, 1), (0, 500), (900, 600)])
def test_voice_payload_clamps_and_invalid_list_becomes_empty(native, monkeypatch, limit, clamped):
    calls = []

    def voices(path, **kwargs):
        calls.append((path, kwargs))
        return {"ok": True, "voices": "not-a-list"}

    monkeypatch.setattr(native.worker, "_android_tts_json_request", voices)
    result = native.worker._android_tts_voices(locale="pt-BR", limit=limit)
    assert calls == [("/native-tts/voices", {"payload": {"locale": "pt-BR", "limit": clamped}, "timeout": 1.2})]
    assert result == {"ok": True, "engine": "android_native", "locale": "pt-BR", "total": 0, "returned": 0, "voices": []}


def test_url_and_version_rebindings_are_visible_after_first_request(native, monkeypatch):
    for url, version in [("http://127.0.0.1:1234///", "fixture-one"), ("http://127.0.0.1:4321", "fixture-two")]:
        monkeypatch.setenv("PHONE_WORKER_ANDROID_TTS_URL", url)
        monkeypatch.setattr(native.worker, "PHONE_WORKER_VERSION", version)
        _, calls = transport(native, monkeypatch, b"{}")
        native.worker._android_tts_json_request("/status")
        request = calls[0][0]
        assert request.full_url == url.rstrip("/") + "/status"
        assert request.get_header("User-agent") == "CorePhoneWorker/" + version
