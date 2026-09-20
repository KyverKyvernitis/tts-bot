"""Real local processes and deterministic interleavings at the PCM IO boundary."""
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cogs.musica.testes.runtime_telefone.ponte_worker.test_streams_pcm import Handler, pcm  # noqa: F401


def wait_file(path, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def local(pcm, monkeypatch, tmp_path):
    worker, clock, _ = pcm
    cache = tmp_path / "audio"
    cache.mkdir()
    monkeypatch.setattr(worker, "_music_pcm_cache_dir", lambda: cache)
    api = SimpleNamespace(**vars(subprocess))
    monkeypatch.setattr(worker, "subprocess", api)
    return SimpleNamespace(worker=worker, clock=clock, cache=cache, control=tmp_path,
                           api=api, children=[])


def start_live(local, monkeypatch, script, output):
    worker = local.worker
    stream_id = worker._register_music_stream({"stream_url": "https://audio.invalid/local"})
    monkeypatch.setattr(worker, "_music_prepared_mode_enabled", lambda: False)
    monkeypatch.setattr(worker, "_music_stream_build_ffmpeg_input_cmd", lambda *a, **kw: [sys.executable, "-u", "-c", script])
    started = threading.Event()

    def popen(cmd, **kwargs):
        kwargs["cwd"] = str(local.control)
        child = subprocess.Popen(cmd, **kwargs)
        local.children.append(child)
        started.set()
        return child

    monkeypatch.setattr(local.api, "Popen", popen)
    handler = Handler(output)
    done = threading.Event()
    failures = []

    def stream():
        try:
            worker._stream_music_pcm(handler, stream_id)
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=stream, daemon=True)
    thread.start()
    assert started.wait(3)
    return handler, thread, done, failures


def stop_children(local, thread):
    for child in local.children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
    thread.join(timeout=3)
    assert not thread.is_alive(), "PCM thread survived process cleanup"


def test_live_stderr_saturation_does_not_block_audio_or_eof(local, monkeypatch):
    script = "import os\nfor _ in range(256): os.write(2, b'e'*8192)\nos.write(1, b'pcm-bytes')\n"
    handler, thread, done, failures = start_live(local, monkeypatch, script, io.BytesIO())
    try:
        completed = done.wait(1.5)
    finally:
        stop_children(local, thread)
    assert completed, "unconsumed stderr blocked stdout before the first PCM bytes"
    assert not failures and handler.responses == [200]
    assert handler.wfile.getvalue() == b"pcm-bytes"
    assert all(child.poll() is not None for child in local.children)
    assert local.children[0].stdout.closed


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError])
def test_live_disconnect_reaps_the_owned_process_and_closes_pipes(local, monkeypatch, error):
    class Output(io.BytesIO):
        def write(self, data):
            raise error("client left")

    script = "import os,time\nos.write(1, b'p'*65536)\ntime.sleep(30)\n"
    handler, thread, done, failures = start_live(local, monkeypatch, script, Output())
    try:
        completed = done.wait(1.5)
    finally:
        stop_children(local, thread)
    assert completed and not failures and handler.responses == [200]
    for child in local.children:
        assert child.poll() is not None
        assert all(pipe is None or pipe.closed for pipe in (child.stdout, child.stderr))


PREPARE_SCRIPT = """
import os, sys, time
from pathlib import Path
target, ready, release = map(Path, sys.argv[1:4])
target.write_bytes(sys.argv[4].encode())
ready.touch()
deadline = time.monotonic() + 6
while not release.exists():
    if time.monotonic() > deadline: sys.exit(71)
    time.sleep(0.005)
for _ in range(128): os.write(2, b'd'*8192)
"""


@pytest.fixture
def preparation(local, monkeypatch):
    state = SimpleNamespace(calls=[], release=local.control / "release", local=local)
    lock = threading.Lock()

    def build(item, *, output):
        with lock:
            index = len(state.calls)
            ready = local.control / ("ready-" + str(index))
            state.calls.append({"output": output, "ready": ready, "title": item.get("title", "track")})
        return [sys.executable, "-u", "-c", PREPARE_SCRIPT, output,
                str(ready), str(state.release), item.get("title", "track")]

    def run(cmd, **kwargs):
        kwargs["cwd"] = str(local.control)
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr(local.worker, "_music_stream_build_ffmpeg_input_cmd", build)
    monkeypatch.setattr(local.api, "run", run)
    yield state
    state.release.touch()


def register(local, title="track"):
    key = local.worker._register_music_stream({"stream_url": "https://audio.invalid/local", "title": title})
    return key, local.worker._music_stream_lookup(key)


def test_same_stream_preparations_share_one_process_and_stable_result(local, preparation):
    key, item = register(local)
    errors, results = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(local.worker._prepare_music_pcm_file, key, dict(item))
        try:
            assert wait_file(local.control / "ready-0")
            second = pool.submit(local.worker._prepare_music_pcm_file, key, dict(item))
        finally:
            preparation.release.touch()
        for future in (first, second):
            try:
                results.append(future.result(timeout=4))
            except Exception as exc:
                errors.append(exc)
    assert len(preparation.calls) == 1, "same stream launched duplicate transcoders"
    assert not errors and len(results) == 2
    assert results[0] == results[1]
    assert Path(results[0]["prepared_pcm_path"]).read_bytes() == b"track"
    assert results[0]["prepared_pcm_bytes"] == 5
    assert local.worker._music_stream_lookup(key)["prepared_pcm_bytes"] == 5
    assert list(local.cache.iterdir()) == [local.cache / (key + ".pcm")]


def test_different_streams_prepare_concurrently_without_global_lock(local, preparation):
    one, two = register(local, "first"), register(local, "second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(local.worker._prepare_music_pcm_file, *entry) for entry in (one, two)]
        try:
            assert wait_file(local.control / "ready-0")
            assert wait_file(local.control / "ready-1"), "one stream blocked unrelated preparation"
            assert not any(future.done() for future in futures)
        finally:
            preparation.release.touch()
        results = [future.result(timeout=4) for future in futures]
    assert [Path(r["prepared_pcm_path"]).read_bytes() for r in results] == [b"first", b"second"]


def test_cache_cleanup_cannot_delete_active_preparation(local, preparation, monkeypatch):
    key, item = register(local)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(local.worker._prepare_music_pcm_file, key, item)
        try:
            assert wait_file(local.control / "ready-0")
            output = Path(preparation.calls[0]["output"])
            with monkeypatch.context() as patch:
                patch.setattr(local.worker, "_music_pcm_cache_max_bytes", lambda: 0)
                local.worker._cleanup_music_pcm_cache()
            retained = output.exists()
        finally:
            preparation.release.touch()
        error = None
        try:
            result = future.result(timeout=4)
        except Exception as exc:
            error = exc
    assert retained, "cache cleanup deleted PCM still owned by a running preparation"
    assert error is None and Path(result["prepared_pcm_path"]).read_bytes() == b"track"


@pytest.mark.parametrize("change", ["expire", "replace"])
def test_obsolete_preparation_cannot_publish_into_expired_or_replaced_stream(local, preparation, change):
    key, item = register(local)
    replacement = {"id": key, "expires_at": 90000, "title": "new owner"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(local.worker._prepare_music_pcm_file, key, item)
        try:
            assert wait_file(local.control / "ready-0")
            if change == "expire":
                local.clock[0] = item["expires_at"]
                assert local.worker._music_stream_lookup(key) is None
            else:
                local.worker._MUSIC_STREAMS = {key: replacement}
        finally:
            preparation.release.touch()
        with pytest.raises(RuntimeError, match="expir|substitu"):
            future.result(timeout=4)
    assert not list(local.cache.iterdir())
    if change == "replace":
        assert local.worker._MUSIC_STREAMS[key] == {"id": key, "expires_at": 90000, "title": "new owner"}


def test_failed_promotion_cleans_own_staging_and_preserves_old_pcm(local, preparation, monkeypatch):
    key, item = register(local)
    target = local.cache / (key + ".pcm")
    target.write_bytes(b"known-good")
    replace = Path.replace

    def fail(path, destination):
        if Path(destination) == target:
            raise OSError("promotion failed")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail)
    preparation.release.touch()
    with pytest.raises(OSError, match="promotion failed"):
        local.worker._prepare_music_pcm_file(key, item)
    assert target.read_bytes() == b"known-good"
    assert list(local.cache.iterdir()) == [target]
    assert "prepared_pcm_path" not in local.worker._music_stream_lookup(key)


def test_real_prepare_timeout_cleans_staging_releases_slot_and_allows_retry(local, preparation, monkeypatch):
    key, item = register(local)
    with monkeypatch.context() as patch:
        patch.setattr(local.worker, "_music_prepare_timeout_seconds", lambda item: 0.05)
        with pytest.raises(subprocess.TimeoutExpired):
            local.worker._prepare_music_pcm_file(key, item)
    assert not list(local.cache.iterdir())
    assert not local.worker._MUSIC_PCM_PREPARATIONS
    preparation.release.touch()
    result = local.worker._prepare_music_pcm_file(key, item)
    assert Path(result["prepared_pcm_path"]).read_bytes() == b"track"
    assert not local.worker._MUSIC_PCM_PREPARATIONS


def test_prepared_response_uses_one_open_file_for_length_and_audio(local):
    target = local.cache / "source.pcm"
    target.write_bytes(b"original")
    replacement = local.cache / "replacement.pcm"
    replacement.write_bytes(b"different bytes")

    class ReplacingHandler(Handler):
        def end_headers(self):
            replacement.replace(target)

    handler = ReplacingHandler(io.BytesIO())
    local.worker._serve_prepared_music_pcm(handler, "file", {"prepared_pcm_path": str(target)})
    assert handler.responses == [200] and handler.headers["Content-Length"] == "8"
    assert handler.wfile.getvalue() == b"original"
    assert target.read_bytes() == b"different bytes"


@pytest.mark.parametrize("fallback", ["false", "true"])
def test_prepared_read_failure_after_headers_does_not_send_another_response(local, monkeypatch, fallback):
    target = local.cache / "source.pcm"
    target.write_bytes(b"original")
    key = local.worker._register_music_stream({"stream_url": "https://audio.invalid/local", "prepared_pcm_path": str(target)})
    monkeypatch.setattr(local.worker, "_music_prepared_mode_enabled", lambda: True)
    monkeypatch.setenv("PHONE_WORKER_MUSIC_PREPARE_LIVE_FALLBACK", fallback)
    opened = Path.open
    monkeypatch.setattr(local.api, "Popen", lambda *a, **kw: pytest.fail("second live response"))

    class BrokenFile:
        def __init__(self, file): self.file = file
        def __enter__(self): return self
        def __exit__(self, *args): self.file.close()
        def fileno(self): return self.file.fileno()
        def read(self, count): raise OSError("disk read failed")

    def open_file(path, *args, **kwargs):
        file = opened(path, *args, **kwargs)
        return BrokenFile(file) if path == target and args == ("rb",) else file

    monkeypatch.setattr(Path, "open", open_file)
    handler = Handler(io.BytesIO())
    local.worker._stream_music_pcm(handler, key)
    assert handler.responses == [200]
    assert handler.close_connection


@pytest.mark.parametrize("fallback", ["false", "true"])
def test_prepare_failure_before_headers_keeps_opt_in_live_fallback(local, monkeypatch, fallback):
    key, _ = register(local)
    monkeypatch.setattr(local.worker, "_music_prepared_mode_enabled", lambda: True)
    monkeypatch.setenv("PHONE_WORKER_MUSIC_PREPARE_LIVE_FALLBACK", fallback)
    monkeypatch.setattr(local.worker, "_prepare_music_pcm_file", lambda *a: (_ for _ in ()).throw(RuntimeError("no cache")))
    monkeypatch.setattr(local.worker, "_music_stream_build_ffmpeg_input_cmd", lambda *a, **kw: ["local"])
    calls = []

    def popen(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=io.BytesIO(b"fallback"), stderr=None,
            kill=lambda: None, wait=lambda **kwargs: None)

    monkeypatch.setattr(local.api, "Popen", popen)
    handler = Handler(io.BytesIO())
    local.worker._stream_music_pcm(handler, key)
    assert handler.responses == ([200] if fallback == "true" else [500])
    assert len(calls) == int(fallback == "true")
    if fallback == "true":
        assert handler.wfile.getvalue() == b"fallback"


def test_pcm_command_preserves_input_headers_codec_and_audio_parameters(local, monkeypatch):
    monkeypatch.setattr(local.worker, "shutil", SimpleNamespace(which=lambda name: "/fixture/ffmpeg"))
    cmd = local.worker._music_stream_build_ffmpeg_input_cmd({"stream_url": "https://audio.invalid/stream",
        "http_headers": {"Cookie": "test=1\r\n", "Host": "discard", "User-Agent": "fixture"}}, output="pipe:1")
    assert cmd == ["/fixture/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
        "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "403,404,408,429,5xx",
        "-reconnect_delay_max", "5", "-rw_timeout", "10000000", "-headers",
        "Cookie: test=1\r\nUser-Agent: fixture\r\n", "-i", "https://audio.invalid/stream",
        "-vn", "-sn", "-dn", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]


def test_warm_pcm_service_observes_rebound_io_clock_registry_and_lock(local, monkeypatch):
    worker = local.worker
    worker._pcm_io()
    directory = local.control / "rebound"
    directory.mkdir()
    monkeypatch.setattr(worker, "_music_pcm_cache_dir", lambda: directory)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: 2500.25))
    monkeypatch.setattr(worker, "_MUSIC_STREAMS", {})
    monkeypatch.setattr(worker, "_MUSIC_PCM_PREPARATIONS", {})

    class Lock:
        depth = 0
        def __enter__(self): self.depth += 1
        def __exit__(self, *args): self.depth -= 1

    lock = Lock()
    monkeypatch.setattr(worker, "_MUSIC_STREAM_LOCK", lock)
    monkeypatch.setattr(worker, "_music_stream_build_ffmpeg_input_cmd", lambda item, **kw: [kw["output"]])
    monkeypatch.setattr(worker, "_music_prepare_timeout_seconds", lambda item: 11.5)
    called = []

    def run(cmd, **kwargs):
        assert lock.depth == 0, "IO ran under the registry lock"
        Path(cmd[0]).write_bytes(b"rebound")
        called.append(kwargs)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(worker, "subprocess", SimpleNamespace(**{**vars(subprocess), "run": run}))
    replace = Path.replace

    def publish(path, destination):
        assert lock.depth > 0, "publication escaped the current registry lock"
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", publish)
    key, item = register(local)
    result = worker._prepare_music_pcm_file(key, item)
    assert result["prepared_pcm_path"] == str(directory / (key + ".pcm"))
    assert result["prepared_at"] == 2500.25 and result["prepared_pcm_bytes"] == 7
    assert worker._MUSIC_STREAMS[key]["prepared_at"] == 2500.25
    assert called[0]["timeout"] == 11.5 and not worker._MUSIC_PCM_PREPARATIONS


def test_warm_prepared_service_uses_live_frame_size_and_file_descriptor(local, monkeypatch):
    worker = local.worker
    worker._pcm_io()
    target = local.cache / "frames.pcm"
    target.write_bytes(b"a" * 1024)
    reads, descriptors = [], []
    opened = Path.open

    class File:
        def __init__(self, file): self.file = file
        def __enter__(self): return self
        def __exit__(self, *args): self.file.close()
        def fileno(self): return self.file.fileno()
        def read(self, count):
            reads.append(count)
            return self.file.read(count)

    def open_file(path, *args, **kwargs):
        file = opened(path, *args, **kwargs)
        return File(file) if path == target and args == ("rb",) else file

    def fstat(fd):
        descriptors.append(fd)
        return os.fstat(fd)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(worker, "os", SimpleNamespace(**{**vars(os), "fstat": fstat}))
    monkeypatch.setattr(worker, "PCM_FRAME_BYTES", 8)
    handler = Handler(io.BytesIO())
    worker._serve_prepared_music_pcm(handler, "frames", {"prepared_pcm_path": str(target)})
    assert reads == [512, 512, 512] and len(descriptors) == 1
    assert handler.headers["Content-Length"] == "1024" and handler.wfile.getvalue() == b"a" * 1024


def test_missing_pcm_module_returns_one_live_error_without_starting_process(local, monkeypatch):
    worker = local.worker
    key, _ = register(local)
    monkeypatch.setattr(worker, "_music_prepared_mode_enabled", lambda: False)
    monkeypatch.setattr(worker, "_pcm_io", lambda: (_ for _ in ()).throw(RuntimeError("pcm_io ausente")))
    monkeypatch.setattr(local.api, "Popen", lambda *a, **kw: pytest.fail("missing module started process"))
    handler = Handler(io.BytesIO())
    worker._stream_music_pcm(handler, key)
    assert handler.responses == [500]
