"""Piper phase 1: compatible with Termux CLI, no resident process or APK."""
from __future__ import annotations

import array
import os
import sys
import wave
from pathlib import Path

import pytest

from test_phone_worker_tts_lifecycle import tts


def _fake_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "piper"
    executable.write_text("""#!/usr/bin/env python3
import array, pathlib, sys
args = sys.argv[1:]
assert '-m' in args and '-f' in args and '--' in args
out = pathlib.Path(args[args.index('-f') + 1])
samples = array.array('f', [0.25] * 2500)
out.write_bytes(samples.tobytes())
""")
    executable.chmod(0o755)
    return executable


def _model(tmp_path: Path) -> Path:
    model = tmp_path / "pt_BR-faber-medium.onnx"
    model.write_bytes(b"fake-model")
    (tmp_path / (model.name + ".json")).write_text('{"audio":{"sample_rate":22050}}')
    return model


def _stage_current_voices(tmp_path, monkeypatch, model):
    import shutil
    directory = tmp_path / "piper-termux-teste" / "voices"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("pt_BR-miro-high", "pt_BR-dii-medium"):
        shutil.copyfile(model, directory / (name + ".onnx"))
        shutil.copyfile(Path(str(model) + ".json"), directory / (name + ".onnx.json"))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_termux_piper_binary_generates_valid_wave(tmp_path, monkeypatch):
    import importlib.util
    path = Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker_runtime/tts_piper.py"
    spec = importlib.util.spec_from_file_location("tts_piper_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    synthesize_to_wav = module.synthesize_to_wav

    model = _model(tmp_path)
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    destination = tmp_path / "speech.wav"
    synthesize_to_wav("Olá, pessoal", str(model), str(model) + ".json", destination, timeout_seconds=8)
    with wave.open(str(destination), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 22050
        assert wav.getnframes() == 2500


def test_piper_task_runs_in_python_with_native_rust_enabled(tts, tmp_path, monkeypatch):
    # The native Rust worker deliberately does not synthesize Piper.
    _fake_cli(tmp_path)
    model = _model(tmp_path)
    import shutil
    voices = tmp_path / "piper-termux-teste" / "voices"
    voices.mkdir(parents=True)
    for name in ("pt_BR-miro-high", "pt_BR-dii-medium"):
        shutil.copyfile(model, voices / f"{name}.onnx")
        shutil.copyfile(Path(str(model) + ".json"), voices / f"{name}.onnx.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(tmp_path / "piper"))
    monkeypatch.setenv("PHONE_WORKER_PIPER_MODEL", str(model))
    monkeypatch.setenv("PHONE_WORKER_PIPER_CONFIG", str(model) + ".json")
    monkeypatch.setenv("PHONE_WORKER_PIPER_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_PIPER_OUTPUT_FORMAT", "wav")
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setattr(tts.worker, "_native_tts_task", lambda *_args: pytest.fail("Piper delegado ao Rust"))
    tts.handler.server.max_output_bytes = 1024 * 1024
    result = tts.handler._task_tts_synthesize_piper({
        "text": "Teste Piper", "engine": "piper", "cache_enabled": False,
        "timeout_seconds": 10, "max_audio_bytes": 1024 * 1024,
    })
    assert result["ok"] and result["engine"] == "piper"
    assert result["audio_format"] == "wav"
    import base64
    payload = base64.b64decode(result["data_b64"])
    assert payload.startswith(b"RIFF") and len(payload) > 44


def test_disabled_piper_is_not_synthesized(tts, tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_PIPER_ENABLED", "false")
    with pytest.raises(RuntimeError, match="desativado"):
        tts.handler._task_tts_synthesize_piper({"text": "Olá"})


def _fake_native_library(tmp_path: Path) -> tuple[Path, Path]:
    import shutil
    import subprocess
    if shutil.which("cc") is None:
        pytest.skip("compiler C indisponível para simular libpiper")
    library = tmp_path / "libpiper.so"
    counter = tmp_path / "loads.txt"
    source = tmp_path / "piper_fake.c"
    source.write_text(r'''
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
struct Voice { float samples[2000]; int cursor; };
struct Chunk { const float *samples; size_t num_samples; int sample_rate; bool is_last; };
void *piper_create(const char *model, const char *cfg, const char *espeak) {
    const char *p = getenv("PIPER_TEST_LOADS_FILE");
    if (p) { FILE *f = fopen(p, "ab"); if (f) { fputc('L', f); fclose(f); } }
    struct Voice *v = calloc(1, sizeof(struct Voice));
    if (!v) return NULL;
    for (int i=0; i<2000; i++) v->samples[i] = 0.125f;
    return v;
}
void piper_free(void *voice) { free(voice); }
int piper_synthesize_start(void *voice, const char *text, const void *opts) {
    ((struct Voice*)voice)->cursor = 0;
    return (text && strlen(text)) ? 0 : -1;
}
int piper_synthesize_next(void *voice, struct Chunk *chunk) {
    struct Voice *v = voice;
    if (v->cursor == 1 && getenv("PIPER_TEST_DELAY_SECOND")) usleep(800000);
    if (v->cursor >= 2) { chunk->is_last=true; return 1; }
    chunk->samples=v->samples; chunk->num_samples=2000;
    chunk->sample_rate=22050; chunk->is_last=v->cursor == 1;
    v->cursor++;
    return 0;
}
''')
    subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    return library, counter


def _resident_module():
    import importlib.util
    path = Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker_runtime/tts_piper.py"
    spec = importlib.util.spec_from_file_location("piper_resident_isolated", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_piper_resident_keeps_one_model_for_repeated_requests(tmp_path, monkeypatch):
    """A resident process loads libpiper once and emits bit-valid 16-bit WAVs."""
    import wave
    library, counter = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PIPER_TEST_LOADS_FILE", str(counter))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    monkeypatch.setenv("PHONE_WORKER_PIPER_RESIDENT_ENABLED", "true")
    module = _resident_module()
    try:
        for number in (1, 2):
            dest = tmp_path / f"resident-{number}.wav"
            module.synthesize_to_wav("Olá Piper", str(model), str(model) + ".json", dest)
            with wave.open(str(dest)) as wav:
                assert wav.getframerate() == 22050
                assert wav.getnframes() == 4000
                assert wav.getsampwidth() == 2
        assert counter.read_text() == "L"
    finally:
        module.stop_resident()


def test_piper_resident_recovers_after_stream_disconnect(tmp_path, monkeypatch):
    library, counter = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PIPER_TEST_LOADS_FILE", str(counter))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    module = _resident_module()
    try:
        source = module.stream_pcm("incompleto", str(model), str(model) + ".json")
        rate, pcm = next(source)
        assert rate == 22050 and len(pcm) == 4000
        source.close()
        assert all(resident.proc is None for resident in module._RESIDENTS.values())
        dest = tmp_path / "after.wav"
        module.synthesize_to_wav("nova frase", str(model), str(model) + ".json", dest)
        assert dest.stat().st_size > 44
        assert counter.read_text() == "LL"
    finally:
        module.stop_resident()


def test_piper_worker_progressive_http_with_native_rust_enabled(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.request import Request, urlopen
    import importlib.util
    import io
    import json
    import struct
    import wave

    library, counter = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    _fake_cli(tmp_path)
    _stage_current_voices(tmp_path, monkeypatch, model)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PIPER_TEST_LOADS_FILE", str(counter))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(tmp_path / "piper"))
    monkeypatch.setenv("PHONE_WORKER_PIPER_MODEL", str(model))
    monkeypatch.setenv("PHONE_WORKER_PIPER_CONFIG", str(model) + ".json")
    monkeypatch.setenv("PHONE_WORKER_PIPER_RESIDENT_ENABLED", "true")
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    phone = Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker.py"
    spec = importlib.util.spec_from_file_location("worker_piper_stream_fixture", phone)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    with ThreadingHTTPServer(("127.0.0.1", 0), worker.WorkerHandler) as server:
        server.worker_token = "secret"
        server.max_body_bytes = 8192
        server.max_output_bytes = 1024 * 1024
        server.job_timeout = 20
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/piper/synthesize.stream",
                data=json.dumps({"text": "Olá Piper", "model_name": "pt_BR-faber-medium"}).encode(),
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=15) as response:
                assert response.status == 200
                assert response.headers["X-Core-Worker-Audio-Format"] == "wav"
                audio = response.read()
            assert audio[:4] == b"RIFF"
            assert struct.unpack("<I", audio[40:44])[0] == 0xFFFFFFFF
            with wave.open(io.BytesIO(audio), "rb") as wav:
                assert len(wav.readframes(99999)) == 8000
            assert counter.read_text() == "L"
        finally:
            server.shutdown()
            thread.join(timeout=3)
            worker._phone_worker_tts_piper_module().stop_resident()


def test_piper_http_delivers_first_audio_before_native_finishes(tmp_path, monkeypatch):
    """HTTP streaming must not buffer the complete WAV before first delivery."""
    import importlib.util
    import json
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer
    from threading import Thread
    library, counter = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    _fake_cli(tmp_path)
    _stage_current_voices(tmp_path, monkeypatch, model)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(tmp_path / "piper"))
    monkeypatch.setenv("PHONE_WORKER_PIPER_MODEL", str(model))
    monkeypatch.setenv("PHONE_WORKER_PIPER_CONFIG", str(model) + ".json")
    monkeypatch.setenv("PIPER_TEST_LOADS_FILE", str(counter))
    monkeypatch.setenv("PIPER_TEST_DELAY_SECOND", "1")
    spec = importlib.util.spec_from_file_location("worker_piper_timing_fixture", Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    with ThreadingHTTPServer(("127.0.0.1", 0), worker.WorkerHandler) as server:
        server.worker_token = "secret"
        server.max_body_bytes = 8192
        server.max_output_bytes = 1024 * 1024
        server.job_timeout = 20
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/piper/synthesize.stream",
                data=json.dumps({"text": "Olá Piper"}).encode(),
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=10) as response:
                prefix = response.read(44 + 4000)
                first_at = time.monotonic() - started
                rest = response.read()
                total_at = time.monotonic() - started
            assert prefix[:4] == b"RIFF" and len(prefix) == 4044
            assert len(rest) == 4000
            assert first_at < 0.65 and total_at > 0.65
        finally:
            server.shutdown()
            thread.join(timeout=3)
            worker._phone_worker_tts_piper_module().stop_resident()


def test_vps_materializes_streamed_wav_with_final_riff_sizes(tmp_path):
    """Streaming's indefinite WAV header must be finalized before cache/file use."""
    import ast
    import asyncio
    import struct
    import types
    import wave
    source = (Path(__file__).parents[1] / "cogs/tts/streaming.py").read_text()
    tree = ast.parse(source)
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SharedSynthesisMixin")
    method = next(node for node in klass.body if getattr(node, "name", "") == "_materialize_shared_job")
    namespace = {"os": os, "struct": struct, "contextlib": __import__("contextlib")}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<piper-cache-method>", "exec"), namespace)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ",
        16, 1, 1, 22050, 44100, 2, 16, b"data", 0xFFFFFFFF)
    payload = header + b"\0\0" * 2000

    class Buffer:
        async def copy_to(self, path):
            Path(path).write_bytes(payload)

    class Fixture:
        def _make_runtime_temp_file(self, *, suffix):
            assert suffix == ".wav"
            return str(tmp_path / "materialized.wav")
        def _get_edge_stream_handles(self):
            return {}

    job = types.SimpleNamespace(item=types.SimpleNamespace(engine="piper"), path="", buffer=Buffer())
    path = asyncio.run(namespace["_materialize_shared_job"](Fixture(), job, cache=False))
    with wave.open(path, "rb") as wav:
        assert wav.getnframes() == 2000
        assert wav.getframerate() == 22050
    data = Path(path).read_bytes()
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
    assert struct.unpack("<I", data[40:44])[0] == len(data) - 44


def test_piper_stream_recovery_preserves_requested_engine():
    """A failed Piper FIFO must retry Piper's complete-file route, not gTTS."""
    import ast
    import asyncio
    from types import SimpleNamespace

    path = Path(__file__).parents[1] / "cogs/tts/audio.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTS") if any(
        isinstance(node, ast.ClassDef) and node.name == "TTS" for node in tree.body
    ) else next(node for node in tree.body if isinstance(node, ast.ClassDef) and any(
        getattr(method, "name", "") == "_tts_local_complete_file_after_stream_failure" for method in node.body
    ))
    method = next(node for node in cls.body if getattr(node, "name", "") == "_tts_local_complete_file_after_stream_failure")
    namespace = {"QueueItem": object}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<piper-retry>", "exec"), namespace)

    class Fixture:
        async def _generate_piper_worker_file(self, item):
            assert item.engine == "piper"
            return "/tmp/fake-piper.wav"

    item = SimpleNamespace(engine="piper")
    assert asyncio.run(namespace[method.name](Fixture(), item)) == "/tmp/fake-piper.wav"


def test_vps_piper_worker_http_client_receives_valid_progressive_wav(tmp_path, monkeypatch):
    """Exercise the real VPS shared-stream HTTP method against the real worker handler."""
    import aiohttp
    import ast
    import asyncio
    import importlib
    import importlib.util
    import json
    import sys
    import time
    import types
    from http.server import ThreadingHTTPServer
    from threading import Thread

    # Some legacy test modules insert a minimal aiohttp stub at collection.
    # The actual HTTP transport integration must use a real ClientSession.
    if not hasattr(aiohttp.ClientSession, "__aenter__"):
        monkeypatch.delitem(sys.modules, "aiohttp")
        aiohttp = importlib.import_module("aiohttp")

    library, _ = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    _stage_current_voices(tmp_path, monkeypatch, model)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    monkeypatch.setenv("PHONE_WORKER_PIPER_MODEL", str(model))
    monkeypatch.setenv("PHONE_WORKER_PIPER_CONFIG", str(model) + ".json")
    monkeypatch.setenv("PHONE_WORKER_PIPER_RESIDENT_ENABLED", "true")
    phone_path = Path(__file__).parents[1] / "deploy/termux/phone-worker/phone_worker.py"
    spec = importlib.util.spec_from_file_location("worker_piper_vps_fixture", phone_path)
    phone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phone)

    source = (Path(__file__).parents[1] / "cogs/tts/streaming.py").read_text()
    tree = ast.parse(source)
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SharedSynthesisMixin")
    method = next(node for node in klass.body if getattr(node, "name", "") == "_produce_worker_stream_job")

    package = types.ModuleType("piper_stream_client_test")
    package.__path__ = []
    audio_stub = types.ModuleType("piper_stream_client_test.audio")
    audio_stub.PHONE_WORKER_TOKEN = "secret"
    audio_stub.TTS_PIPER_WORKER_TIMEOUT_SECONDS = 20
    audio_stub.TTS_PIPER_MODEL_NAME = "pt_BR-faber-medium"
    audio_stub.aiohttp = aiohttp
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, audio_stub.__name__, audio_stub)
    package.audio = audio_stub
    namespace = {"__package__": package.__name__, "time": time, "asyncio": asyncio}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<vps-piper-client>", "exec"), namespace)

    class Buffer:
        max_bytes = 1024 * 1024
        def __init__(self):
            self.data = bytearray()
        @property
        def size(self):
            return len(self.data)

    class Fixture:
        def __init__(self, url, session):
            self.url = url
            self.session = session
            self.metrics = {}
            self.success = None
        def _phone_worker_tts_base_url(self):
            return self.url
        async def _get_phone_worker_http_session(self):
            return self.session
        def _get_metrics_store(self):
            return self.metrics
        @staticmethod
        def _worker_header_value(headers, key):
            return headers.get(key, "").strip().lower()
        async def _append_shared_audio(self, job, chunk):
            job.buffer.data.extend(chunk)
            return True
        def _record_tts_agent_synth_success(self, **kwargs):
            self.success = kwargs
        def _record_route_engine_result(self, *_args, **_kwargs):
            pass
        def _mark_tts_agent_synth_failure(self, _error):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), phone.WorkerHandler) as server:
        server.worker_token = "secret"
        server.max_body_bytes = 8192
        server.max_output_bytes = 1024 * 1024
        server.job_timeout = 20
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        async def run():
            async with aiohttp.ClientSession() as session:
                target = Fixture(f"http://127.0.0.1:{server.server_address[1]}", session)
                job = types.SimpleNamespace(item=types.SimpleNamespace(engine="piper", text="Teste do VPS"),
                    buffer=Buffer(), deadline=time.monotonic() + 20, actual_engine="piper", route="")
                await namespace[method.name](target, job)
                assert job.route == "worker"
                assert job.actual_engine == "piper"
                assert job.buffer.data[:4] == b"RIFF"
                assert len(job.buffer.data) == 8044
                assert target.success is not None
                assert target.success["data"]["audio_format"] == "wav"
        try:
            asyncio.run(run())
        finally:
            server.shutdown()
            thread.join(timeout=3)
            phone._phone_worker_tts_piper_module().stop_resident()


def test_piper_two_resident_voices_and_reuse(tmp_path, monkeypatch):
    """The two models stay loaded and switching does not recreate either process."""
    library, counter = _fake_native_library(tmp_path)
    model = _model(tmp_path)
    _stage_current_voices(tmp_path, monkeypatch, model)
    monkeypatch.setenv("PHONE_WORKER_PIPER_LIB", str(library))
    monkeypatch.setenv("PIPER_TEST_LOADS_FILE", str(counter))
    monkeypatch.setenv("PHONE_WORKER_PIPER_BIN", str(_fake_cli(tmp_path)))
    monkeypatch.setenv("PHONE_WORKER_PIPER_RESIDENT_ENABLED", "true")
    module = _resident_module()
    voices = tmp_path / "piper-termux-teste" / "voices"
    try:
        assert module.warmup()
        assert len(module._RESIDENTS) == 2
        assert counter.read_text() == "LL"
        for name in ("pt_BR-miro-high", "pt_BR-dii-medium", "pt_BR-miro-high"):
            dest = tmp_path / (name + ".wav")
            module.synthesize_to_wav("Olá", str(voices / (name + ".onnx")),
                                     str(voices / (name + ".onnx.json")), dest)
            assert dest.stat().st_size > 44
        assert counter.read_text() == "LL"
    finally:
        module.stop_resident()


def test_piper_modal_has_only_two_voices():
    source = (Path(__file__).parents[1] / "cogs/tts/interface/modal_piper.py").read_text()
    assert '"pt_BR-miro-high": "Voz masculina"' in source
    assert '"pt_BR-dii-medium": "Voz feminina"' in source
    assert 'VOZES_PIPER = {"pt_BR-faber-medium"' not in source
