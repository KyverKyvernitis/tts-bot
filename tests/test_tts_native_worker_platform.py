"""Real sockets/processes cover native child lifetime and transparent aborts."""
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge():
    module = load("test_native_tts_bridge", PHONE / "phone_worker_runtime/native_tts.py")
    yield module
    if module._SUPERVISOR is not None:
        module._SUPERVISOR.close()


@pytest.fixture
def installer():
    return load("test_native_tts_installer", ROOT / "scripts/install-tts-native.py")


@pytest.fixture
def native_binary():
    path = Path(os.getenv("TTS_NATIVE_WORKER_TEST_BINARY") or ROOT / "target/debug/tts-worker")
    if not path.is_file():
        if os.getenv("TTS_NATIVE_REQUIRED") == "1":
            pytest.fail("TTS_NATIVE_REQUIRED=1 requires the real tts-worker binary")
        pytest.skip("build tts-worker or set TTS_NATIVE_WORKER_TEST_BINARY for native process gates")
    return path.resolve()


def manifest(tmp_path, binary, installer, version="1.0.0"):
    artifact = tmp_path / "tts-worker"
    shutil.copyfile(binary, artifact)
    value = {"schema": installer.SCHEMA, "version": version, "target": installer.current_target(),
             "artifacts": [{"name": "tts-worker", "file": artifact.name, "bytes": artifact.stat().st_size,
                            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    path = tmp_path / f"manifest-{version}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def test_native_flag_defaults_off_without_import_side_effects(bridge, monkeypatch):
    monkeypatch.delenv("TTS_NATIVE_WORKER", raising=False)
    assert not bridge.enabled()
    assert bridge._SUPERVISOR is None
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    assert bridge.enabled()


def test_platform_broker_releases_parent_lock_after_native_disconnect(bridge):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tts-lease-") as temporary:
        path = Path(temporary) / "lease.sock"
        heavy_lock = threading.Lock()
        broker = bridge._ResourceBroker(path, heavy_lock)
        native = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        native.settimeout(2)
        try:
            native.connect(str(path))
            native.sendall(b"acquire\n")
            response = bytearray()
            while len(response) < 3:
                part = native.recv(3 - len(response))
                assert part
                response.extend(part)
            assert response == b"ok\n"
            assert not heavy_lock.acquire(blocking=False)
            # EOF is also the release receipt after native process death.
            native.close()
            assert heavy_lock.acquire(timeout=2)
            heavy_lock.release()
        finally:
            native.close()
            broker.close()
        assert not path.exists()


def test_parent_bridge_configures_the_existing_heavy_resource_lock():
    worker = load("test_native_phone_lease_parent", PHONE / "phone_worker.py")
    module = worker._native_tts_bridge_module()
    assert module._HEAVY_LOCK is worker._HEAVY_RESOURCE_LOCK
    assert module._SUPERVISOR is None


def test_supervisor_runs_real_child_and_closing_owner_ends_it(bridge, native_binary, tmp_path):
    # Unix socket addresses must remain short on both Linux and Android.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tts-owner-") as temporary:
        owner = bridge.Supervisor(binary=native_binary, socket_path=Path(temporary) / "s", cache=tmp_path / "cache")
        try:
            owner.ensure()
            process = owner.process
            assert owner._health()["pid"] == process.pid
            assert owner.ensure() == owner.socket_path()
            assert owner.process is process
            assert owner.socket_path().stat().st_mode & 0o777 == 0o600
            owner.close()
            assert process.poll() is not None
            assert not owner.socket_path().exists()
        finally:
            owner.close()


def test_parent_death_pipe_cancels_worker_without_python_teardown(native_binary, tmp_path):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tts-eof-") as temporary:
        path = Path(temporary) / "s"
        process = subprocess.Popen([str(native_binary), "--socket", str(path), "--cache-dir", str(tmp_path / "cache"), "--owner-stdin"], stdin=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not path.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.02)
            assert path.exists()
            process.stdin.close()
            assert process.wait(timeout=5) == 0
            assert not path.exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_native_artifact_installer_proves_real_http_health(installer, native_binary, tmp_path):
    source, _ = manifest(tmp_path, native_binary, installer)
    root = tmp_path / "runtime"
    result = installer.install(source, root)
    assert result["ok"] and result["restart_required"]
    assert result["health"]["worker_health"] == "ready"
    assert (root / "current/tts-worker").is_file()
    assert not (root / "transaction.json").exists()


def test_bad_hash_never_promotes(installer, tmp_path):
    source, value = manifest(tmp_path, Path(shutil.which("true")), installer)
    value["artifacts"][0]["sha256"] = "0" * 64
    source.write_text(json.dumps(value))
    root = tmp_path / "runtime"
    with pytest.raises(ValueError, match="SHA-256"):
        installer.install(source, root, check=lambda *_: {})
    assert not (root / "current").exists()
    assert not list((root / "releases").glob(".staging-*"))


def test_postpromotion_failure_restores_both_pointers(installer, tmp_path):
    source, _ = manifest(tmp_path, Path(shutil.which("true")), installer)
    root = tmp_path / "runtime"
    installer.install(source, root, check=lambda *_: {"ok": True})
    before = os.readlink(root / "current")
    source, _ = manifest(tmp_path, Path(shutil.which("true")), installer, "2.0.0")

    def reject_promoted(directory, _manifest):
        if directory.name == "current":
            raise RuntimeError("candidate failed health")
        return {"ok": True}

    with pytest.raises(RuntimeError, match="failed health"):
        installer.install(source, root, check=reject_promoted)
    assert os.readlink(root / "current") == before
    assert not (root / "previous").exists()
    assert not (root / "transaction.json").exists()


def test_interrupted_promotion_recovers_before_new_transaction(installer, tmp_path):
    source, _ = manifest(tmp_path, Path(shutil.which("true")), installer)
    root = tmp_path / "runtime"
    installer.install(source, root, check=lambda *_: {})
    before = os.readlink(root / "current")
    installer.atomic_json(root / "transaction.json", {"schema": installer.SCHEMA, "current": before, "previous": None})
    installer.replace_link(root / "previous", before)
    installer.replace_link(root / "current", None)
    installer.recover(root)
    assert os.readlink(root / "current") == before
    assert not (root / "previous").exists()


def test_wrong_target_and_symlink_sources_rejected(installer, tmp_path):
    source, value = manifest(tmp_path, Path(shutil.which("true")), installer)
    wrong = "aarch64-linux-android" if installer.current_target() != "aarch64-linux-android" else "x86_64-unknown-linux-gnu"
    with pytest.raises(ValueError, match="target mismatch"):
        installer.install(source, tmp_path / "runtime", target=wrong, check=lambda *_: {})
    artifact = tmp_path / value["artifacts"][0]["file"]
    artifact.unlink()
    artifact.symlink_to(shutil.which("true"))
    with pytest.raises(ValueError, match="invalid local"):
        installer.install(source, tmp_path / "runtime", check=lambda *_: {})


@pytest.mark.parametrize("ending", [b"0\r\n\r\n", b""])
def test_proxy_preserves_normal_eof_and_aborted_chunked_audio(bridge, tmp_path, monkeypatch, ending):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tts-relay-") as temporary:
        path = Path(temporary) / "s"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        request_seen = []

        def native():
            connection, _ = listener.accept()
            try:
                data = bytearray()
                while b"\r\n\r\n" not in data:
                    data.extend(connection.recv(4096))
                request_seen.append(bytes(data))
                connection.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\nContent-Type: audio/mpeg\r\n\r\n3\r\nmp3\r\n" + ending)
            finally:
                connection.close()

        thread = threading.Thread(target=native)
        thread.start()
        monkeypatch.setattr(bridge, "supervisor", lambda **_: type("Owner", (), {"ensure": lambda self: path})())

        class Handler(BaseHTTPRequestHandler):
            max_body_bytes = 1024
            job_timeout = 3

            def do_POST(self):
                bridge.proxy_http(self, method="POST", path="/tts-agent/synthesize.stream")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            client.request("POST", "/tts-agent/synthesize.stream", b"{}", {"Authorization": "Bearer secret-token", "Content-Type": "application/json"})
            response = client.getresponse()
            assert response.status == 200
            if ending:
                assert response.read() == b"mp3"
            else:
                with pytest.raises(http.client.IncompleteRead):
                    response.read()
            assert b"secret-token" not in request_seen[0]
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            listener.close()
            thread.join(timeout=5)


def test_optin_parent_delegates_synthesis_before_legacy_policy(monkeypatch):
    worker = load("test_native_phone_parent", PHONE / "phone_worker.py")
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    calls = []
    monkeypatch.setattr(worker, "_native_tts_task", lambda body, task: calls.append((body, task)) or {"ok": True})
    monkeypatch.setattr(worker, "_phone_worker_tts_policy_module", lambda: pytest.fail("legacy TTS policy called"))
    handler = object.__new__(worker.WorkerHandler)
    monkeypatch.setattr(handler, "_ensure_tts_piper_turbo_allowed", lambda: ([], []))
    assert handler._task_tts_agent_synthesize({"engine": "teto", "text": "fala"}) == {"ok": True}
    assert calls == [({"engine": "teto", "text": "fala"}, "tts_agent_synthesize")]


@pytest.fixture
def native_ttv(bridge, native_binary, tmp_path, monkeypatch):
    """A compiled native resampler fixture exercises the real Rust pipeline.

    The fixture copies a known WAV; it is not a voice quality or engine test.
    """
    import math
    import struct
    import tempfile
    import wave

    compiler = shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        if os.getenv("TTS_NATIVE_REQUIRED") == "1":
            pytest.fail("native TTV integration fixture requires a C compiler")
        pytest.skip("C compiler required for the native test resampler")
    source = tmp_path / "resampler.c"
    source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
int main(int argc,char **argv){
 if(getenv("TTS_TEST_NATIVE_FAIL")) return 7;
 if(argc<3) return 2;
 FILE *in=fopen(argv[1],"rb"),*out=fopen(argv[2],"wb");
 if(!in||!out) return 3;
 char block[4096];size_t n;
 while((n=fread(block,1,sizeof block,in)))if(fwrite(block,1,n,out)!=n)return 4;
 return fclose(in)||fclose(out);
}
''')
    engine = tmp_path / "native-resampler"
    subprocess.run([compiler, str(source), "-O2", "-o", str(engine)], check=True, capture_output=True)
    bank = tmp_path / "bank"
    bank.mkdir()
    with wave.open(str(bank / "tone.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"".join(struct.pack("<h", round(12000 * math.sin(i * 2 * math.pi * 220 / 44100))) for i in range(22050)))
    aliases = ["a", "- a", "a -", "i", "- i", "i -", "あ", "い", "a i", "i a"]
    (bank / "oto.ini").write_text("\n".join(f"tone.wav={alias},0,30,-450,30,10" for alias in aliases), encoding="utf-8")
    for key, value in {
        "PHONE_WORKER_TETO_ENABLED": "true", "PHONE_WORKER_TETO_BACKEND": "utau",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank), "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(bank),
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "standard", "PHONE_WORKER_TETO_MIN_ALIASES": "1",
        "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1", "PHONE_WORKER_TETO_NATIVE_RESAMPLER_COMMAND": str(engine),
        "PHONE_WORKER_TETO_NATIVE_BACKEND": "command", "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(tmp_path / "fragments"),
        "PHONE_WORKER_MIKU_ENABLED": "false", "PHONE_WORKER_ANNA_ENABLED": "false", "PHONE_WORKER_PURURU_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    with tempfile.TemporaryDirectory(prefix="tts-ttv-") as temporary:
        owner = bridge.Supervisor(binary=native_binary, socket_path=Path(temporary) / "s", cache=tmp_path / "cache", heavy_lock=threading.Lock())
        owner.ensure()
        yield owner
        owner.close()


def ttv_request(bridge, native_ttv, path, body):
    connection = bridge._UnixHTTP(native_ttv.socket_path(), 10)
    connection.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return connection, connection.getresponse()


def performance_body():
    score = {"version": 1, "track_index": 0, "duration_ms": 1200, "lyrics_source": "message", "notes": [
        {"midi_note": 60, "start_ms": 100, "duration_ms": 450, "velocity": 100},
        {"midi_note": 64, "start_ms": 600, "duration_ms": 450, "velocity": 100},
    ]}
    body = {"engine": "teto", "text": "a i", "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR", "ttv_score": score,
            "timeout_seconds": 10, "max_audio_bytes": 2 * 1024 * 1024, "cache_mode": "bypass"}
    identity = {"version": 1, "text": "a i", "score": score, "segments": None}
    body["ttv_performance_hash"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return body


def test_real_ttv_speech_raw_and_json_keep_controls(bridge, native_ttv):
    import base64
    body = {"engine": "teto", "text": "a", "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR",
            "ttv_pitch_semitones": 0.0, "ttv_speech_rate": 1.0, "cache_mode": "bypass"}
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", body)
    try:
        raw = response.read()
        assert response.status == 200, raw
        assert raw.startswith(b"RIFF")
        assert response.getheader("X-Core-Worker-TTV-Voice-Id") == "kasane-teto"
        assert response.getheader("X-Core-Worker-TTV-Pitch-Semitones") == "0.0"
        assert response.getheader("X-Core-Worker-Sha256") == hashlib.sha256(raw).hexdigest()
    finally:
        connection.close()
    connection, response = ttv_request(bridge, native_ttv, "/task", dict(body, task="tts_agent_synthesize"))
    try:
        result = json.loads(response.read())
        assert response.status == 200, result
        assert result["engine"] == "teto" and result["ttv_voice_id"] == "kasane-teto"
        assert base64.b64decode(result["data_b64"]) == raw
        assert result["ttv_pitch_semitones"] == 0.0
    finally:
        connection.close()


def test_real_ttv_singing_stream_has_metadata_pcm_receipt_and_normal_eof(bridge, native_ttv):
    import struct
    body = performance_body()
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/singing.pcm", body)
    try:
        data = response.read()
        assert response.status == 200, data
        assert response.getheader("X-Core-Worker-TTV-PCM-Protocol") == "1"
        records = []
        while data:
            assert len(data) >= 5
            kind, size = data[:1], struct.unpack("!I", data[1:5])[0]
            assert len(data) >= 5 + size
            records.append((kind, data[5:5 + size]))
            data = data[5 + size:]
        assert records[0][0] == b"M" and records[-1][0] == b"E"
        metadata = json.loads(records[0][1])
        pcm = b"".join(value for kind, value in records if kind == b"P")
        receipt = json.loads(records[-1][1])
        assert len(pcm) == metadata["expected_frames"] * 2 == receipt["pcm_bytes"]
        assert receipt["pcm_sha256"] == hashlib.sha256(pcm).hexdigest()
        assert metadata["ttv_performance_hash"] == body["ttv_performance_hash"]
        assert metadata["normalization_gain"] == 1.0
    finally:
        connection.close()


def test_missing_character_is_explicit_and_status_does_not_advertise_it(bridge, native_ttv):
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", {"engine": "teto", "text": "a", "ttv_voice_id": "hatsune-miku"})
    try:
        result = json.loads(response.read())
        assert response.status == 400 and "MIKU_ENABLED" in result["error"]
    finally:
        connection.close()
    status = native_ttv._health()
    assert "teto" in status["available_engines"]
    voices = {voice["voice_id"]: voice for voice in status["teto"]["ttv_voices"]}
    assert voices["kasane-teto"]["ready"]
    assert not voices["hatsune-miku"]["ready"]


def test_real_ttv_engine_failure_after_metadata_has_no_receipt_or_http_eof(bridge, native_ttv, monkeypatch):
    # Start a fresh real worker with a deliberately failing native executable.
    # The normal stream emits its metadata before invoking that executable.
    monkeypatch.setenv("TTS_TEST_NATIVE_FAIL", "1")
    with native_ttv._lock:
        native_ttv._stop_locked()
    native_ttv.ensure()
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/singing.pcm", performance_body())
    try:
        assert response.status == 200
        with pytest.raises(http.client.IncompleteRead) as failure:
            response.read()
        partial = failure.value.partial
        assert partial.startswith(b"M")
        import struct
        length = struct.unpack("!I", partial[1:5])[0]
        assert len(partial) == length + 5  # no PCM/end receipt fabricated
    finally:
        connection.close()
    assert native_ttv.heavy_lock.acquire(timeout=2), "failed native job retained the platform lease"
    native_ttv.heavy_lock.release()


def test_ttv_status_fingerprints_are_usable_for_native_cache_only(bridge, native_ttv):
    body = {"engine": "teto", "text": "a", "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR"}
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", body)
    try:
        original = response.read()
        assert response.status == 200, original
        assert response.getheader("X-Core-Worker-Cache-Hit") == "false"
    finally:
        connection.close()
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", dict(body, cache_mode="only"))
    try:
        assert response.status == 200, response.read()
        assert response.getheader("X-Core-Worker-Cache-Hit") == "true"
        assert response.read() == original
    finally:
        connection.close()


def test_ttv_retains_parent_build_and_maintenance_lease(bridge, native_ttv):
    assert native_ttv.heavy_lock.acquire(blocking=False)
    body = {"engine": "teto", "text": "a", "cache_mode": "bypass"}
    try:
        connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", body)
        try:
            result = json.loads(response.read())
            assert response.status == 503 and "build ou manutenção" in result["error"]
        finally:
            connection.close()
    finally:
        native_ttv.heavy_lock.release()
    connection, response = ttv_request(bridge, native_ttv, "/tts-agent/synthesize.raw", body)
    try:
        assert response.status == 200, response.read()
        assert response.read().startswith(b"RIFF")
    finally:
        connection.close()
    assert native_ttv.heavy_lock.acquire(timeout=1), "physical lease not returned"
    native_ttv.heavy_lock.release()
