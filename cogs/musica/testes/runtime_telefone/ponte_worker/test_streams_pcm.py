"""PCM musical do Phone Worker: registry, arquivos preparados e streaming."""
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

@pytest.fixture
def pcm(monkeypatch, tmp_path):
    from cogs.musica.runtime_telefone.ponte_worker import streams as music

    music._MUSIC_STREAMS.clear()
    music._MUSIC_PCM_PREPARATIONS.clear()
    clock = [1000.0]
    monkeypatch.setattr(music, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(music, "_music_pcm_cache_dir", lambda: tmp_path)
    monkeypatch.delenv("PHONE_WORKER_MUSIC_STREAM_TTL_SECONDS", raising=False)
    monkeypatch.delenv("PHONE_WORKER_MUSIC_PREPARE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PHONE_WORKER_MUSIC_PREPARE_MAX_DURATION_SECONDS", raising=False)
    return music, clock, tmp_path


@pytest.mark.parametrize(("configured", "ttl"), [("1", 300), ("999999", 21600), ("invalid", 7200)])
def test_stream_registry_copies_records_and_expires_with_live_owner(pcm, monkeypatch, configured, ttl):
    worker, clock, cache = pcm
    monkeypatch.setenv("PHONE_WORKER_MUSIC_STREAM_TTL_SECONDS", configured)
    prepared = cache / "prepared.pcm"
    prepared.write_bytes(b"pcm")
    original = {"direct_url": "https://audio.invalid/fixture", "title": "fixture", "prepared_pcm_path": str(prepared)}
    stream_id = worker._register_music_stream(original)
    assert len(stream_id) == 32 and int(stream_id, 16) > 0 and "id" not in original
    item = worker._music_stream_lookup(stream_id)
    assert item["expires_at"] - item["created_at"] == ttl
    item["title"] = "changed outside registry"
    assert worker._music_stream_lookup(stream_id)["title"] == "fixture"
    lock = worker._MUSIC_STREAM_LOCK
    live = worker._MUSIC_STREAMS
    clock[0] += ttl
    assert worker._music_stream_lookup(stream_id) is None and not prepared.exists()
    assert worker._MUSIC_STREAM_LOCK is lock and worker._MUSIC_STREAMS is live
    worker._MUSIC_STREAMS = {"new": {"id": "new", "expires_at": clock[0] + 10}}
    assert worker._music_stream_lookup(" new ")["id"] == "new"
    assert worker._register_music_stream({}) == "" and worker._music_stream_lookup("") is None


def test_prepared_cleanup_does_not_remove_outside_cache(pcm, tmp_path):
    worker, _, cache = pcm
    outside = tmp_path.parent / (tmp_path.name + "-outside.pcm")
    outside.write_bytes(b"outside")
    try:
        worker._cleanup_music_prepared_file({"prepared_pcm_path": str(outside)})
        assert outside.read_bytes() == b"outside"
        link = cache / "outside-link.pcm"
        link.symlink_to(outside)
        worker._cleanup_music_prepared_file({"prepared_pcm_path": str(link)})
        assert outside.read_bytes() == b"outside"
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize("failure", ["timeout", "spawn", "returncode", "empty"])
def test_prepare_failure_removes_temporary_and_preserves_previous_pcm(pcm, monkeypatch, failure):
    worker, _, cache = pcm
    target, temporary = cache / "stream.pcm", cache / "stream.tmp.pcm"
    target.write_bytes(b"known-good")
    monkeypatch.setattr(worker, "_music_stream_build_ffmpeg_input_cmd", lambda item, **kw: ["ffmpeg-fixture", kw["output"]])
    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"partial" if failure != "empty" else b"")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ffmpeg-fixture", 45)
        if failure == "spawn":
            raise OSError("could not start process")
        return SimpleNamespace(returncode=1 if failure == "returncode" else 0, stderr=b"fixture failure")
    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises((subprocess.TimeoutExpired, OSError, RuntimeError)):
        worker._prepare_music_pcm_file("stream", {"stream_url": "https://audio.invalid/fixture"})
    assert not temporary.exists() and target.read_bytes() == b"known-good"
    assert list(cache.iterdir()) == [target]


def test_preparation_reuses_valid_cache_and_publishes_pcm_metadata(pcm, monkeypatch):
    worker, _, cache = pcm
    item = {"stream_url": "https://audio.invalid/fixture", "duration": 1}
    stream_id = worker._register_music_stream(item)
    calls = []
    monkeypatch.setattr(worker, "_music_stream_build_ffmpeg_input_cmd", lambda item, **kw: [kw["output"]])
    def run(cmd, **kw):
        calls.append(kw)
        Path(cmd[0]).write_bytes(b"\0" * 192000)
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(worker.subprocess, "run", run)
    result = worker._prepare_music_pcm_file(stream_id, worker._music_stream_lookup(stream_id))
    assert result["prepared_pcm_bytes"] == 192000 and result["prepared_pcm_seconds"] == 1.0
    assert worker._music_stream_lookup(stream_id)["stream_mode"] == "prepared_pcm"
    assert worker._prepare_music_pcm_file(stream_id, result) is result and len(calls) == 1
    assert not list(cache.glob("*.tmp.pcm"))


class Handler:
    def __init__(self, output):
        self.wfile = output
        self.responses, self.headers = [], {}
        self._headers_buffer = []
    def send_response(self, code): self.responses.append(code)
    def send_header(self, key, value): self.headers[key] = value
    def end_headers(self): pass


@pytest.mark.parametrize("mode", ["complete", "disconnect", "read_error"])
def test_live_stream_closes_process_and_pipes_without_second_response(pcm, monkeypatch, mode):
    worker, _, _ = pcm
    stream_id = worker._register_music_stream({"stream_url": "https://audio.invalid/fixture"})
    monkeypatch.setattr(worker, "_music_prepared_mode_enabled", lambda: False)
    monkeypatch.setattr(worker, "_music_stream_build_ffmpeg_input_cmd", lambda *a, **kw: ["ffmpeg-fixture"])
    class Output(io.BytesIO):
        def write(self, data):
            if mode == "disconnect": raise BrokenPipeError("client disconnected")
            return super().write(data)
    class Input(io.BytesIO):
        def read(self, count):
            if mode == "read_error": raise OSError("pipe read failed")
            return super().read(count)
    stdout, stderr = Input(b"x" * worker.PCM_FRAME_BYTES), io.BytesIO()
    calls = []
    proc = SimpleNamespace(stdout=stdout, stderr=stderr, kill=lambda: calls.append("kill"),
                           wait=lambda **kw: calls.append(("wait", kw["timeout"])))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: proc)
    handler = Handler(Output())
    worker._stream_music_pcm(handler, stream_id)
    assert stdout.closed and stderr.closed and calls == ["kill", ("wait", 2)]
    assert handler.responses == [200]
    assert handler.headers["X-Core-Worker-Stream-Mode"] == "live-pcm"
    if mode == "complete": assert handler.wfile.getvalue() == b"x" * worker.PCM_FRAME_BYTES


def test_prepared_stream_preserves_pcm_bytes_and_frame_parameters(pcm):
    worker, _, cache = pcm
    path = cache / "fixture.pcm"
    data = bytes(range(256)) * 1500
    path.write_bytes(data)
    handler = Handler(io.BytesIO())
    worker._serve_prepared_music_pcm(handler, "fixture", {"prepared_pcm_path": str(path), "prepared_pcm_seconds": 2.0})
    assert handler.wfile.getvalue() == data and handler.responses == [200]
    assert handler.headers["Content-Length"] == str(len(data))
    assert handler.headers["X-Core-Worker-Stream-Mode"] == "prepared-pcm"
    assert (worker.PCM_SAMPLE_RATE, worker.PCM_CHANNELS, worker.PCM_SAMPLE_WIDTH_BYTES, worker.PCM_FRAME_MS) == (48000, 2, 2, 20)
