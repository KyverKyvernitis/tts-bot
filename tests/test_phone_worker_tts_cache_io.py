"""Real cache files, kernel locks and controlled publication interleavings."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import tts  # noqa: F401


def cache_file(tts, name, data=b"old-audio", mtime=100):
    tts.root.mkdir(parents=True, exist_ok=True)
    path = tts.root / name
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def after_scan(tts, monkeypatch, action):
    original = os.scandir

    class Scan:
        def __enter__(self):
            def entries():
                with original(tts.root) as items:
                    yield from items
                action()
            return entries()

        def __exit__(self, *args):
            return False

    local_os = SimpleNamespace(**vars(os))
    local_os.scandir = lambda root: Scan()
    monkeypatch.setattr(tts.worker, "os", local_os)


@pytest.mark.parametrize("limits", [(1000, 2), (9, 1000)])
def test_prune_oldest_audio_until_both_limits_allow_remaining_files(tts, limits):
    oldest = cache_file(tts, "oldest.mp3", b"aa", 100)
    older = cache_file(tts, "older.wav", b"bbb", 200)
    retained = cache_file(tts, "retained.ogg", b"cccc", 300)
    fresh = cache_file(tts, "fresh.wav", b"ddddd", 950)
    ignored = cache_file(tts, "upload.mp3.part", b"unfinished", 0)
    link = tts.root / "link.mp3"
    link.symlink_to(retained)
    directory = tts.root / "directory.wav"
    directory.mkdir()
    tts.worker._prune_audio_cache(tts.root, *limits)
    assert not oldest.exists() and not older.exists()
    assert retained.read_bytes() == b"cccc" and fresh.read_bytes() == b"ddddd"
    assert ignored.read_bytes() == b"unfinished" and link.is_symlink() and directory.is_dir()


def test_prune_keeps_protected_and_fresh_files_even_over_budget(tts):
    protected = cache_file(tts, "protected.wav", mtime=0)
    boundary = cache_file(tts, "boundary.mp3", mtime=820)
    fresh = cache_file(tts, "fresh.ogg", mtime=821)
    tts.worker._prune_audio_cache(tts.root, 0, 0, protected=protected)
    assert protected.exists() and fresh.exists() and not boundary.exists()


def test_prune_does_not_remove_shared_locked_stream_and_reclaims_after_close(tts):
    target = cache_file(tts, "stream.mp3")
    with target.open("rb") as reader:
        fcntl.flock(reader.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        tts.worker._prune_audio_cache(tts.root, 0, 0)
        assert target.exists() and reader.read() == b"old-audio"
    tts.worker._prune_audio_cache(tts.root, 0, 0)
    assert not target.exists()


def test_prune_keeps_replacement_published_after_scan_before_open(tts, monkeypatch):
    target = cache_file(tts, "replace.wav")
    replacement = cache_file(tts, "replacement.part", b"complete-new-audio", 950)
    after_scan(tts, monkeypatch, lambda: os.replace(replacement, target))
    tts.worker._prune_audio_cache(tts.root, 0, 0)
    assert target.read_bytes() == b"complete-new-audio"


def test_prune_keeps_file_touched_after_scan(tts, monkeypatch):
    target = cache_file(tts, "recently-used.wav")
    after_scan(tts, monkeypatch, lambda: os.utime(target, (1000, 1000)))
    tts.worker._prune_audio_cache(tts.root, 0, 0)
    assert target.read_bytes() == b"old-audio"


def test_prune_keeps_replacement_published_after_open(tts, monkeypatch):
    target = cache_file(tts, "replace-open.wav")
    replacement = cache_file(tts, "replacement.part", b"new-inode", 950)
    local_os = SimpleNamespace(**vars(os))

    def fstat(fd):
        result = os.fstat(fd)
        os.replace(replacement, target)
        return result

    local_os.fstat = fstat
    monkeypatch.setattr(tts.worker, "os", local_os)
    tts.worker._prune_audio_cache(tts.root, 0, 0)
    assert target.read_bytes() == b"new-inode"


def test_prune_tolerates_disappearance_after_scan(tts, monkeypatch):
    target = cache_file(tts, "gone.wav")
    after_scan(tts, monkeypatch, target.unlink)
    assert tts.worker._prune_audio_cache(tts.root, 0, 0) is None


@pytest.mark.parametrize("missing", [True, False])
def test_prune_tolerates_unavailable_directory(tts, monkeypatch, missing):
    if not missing:
        local_os = SimpleNamespace(**vars(os))
        local_os.scandir = lambda *a: (_ for _ in ()).throw(PermissionError("controlled"))
        monkeypatch.setattr(tts.worker, "os", local_os)
    assert tts.worker._prune_audio_cache(tts.root, 0, 0) is None


def publish(tts, kind, data, key="a" * 64):
    if kind == "direct":
        return tts.handler._task_tts_cache_store({"cache_key": key, "audio_format": "wav",
            "data_b64": base64.b64encode(data).decode()})
    logs = []
    tts.handler._store_tts_agent_standard_cache(key=key, data=data, audio_format="wav", logs=logs)
    return logs


@pytest.mark.parametrize("kind", ["direct", "standard"])
def test_concurrent_publications_expose_only_complete_old_or_new_audio(tts, monkeypatch, kind):
    target = cache_file(tts, "a" * 64 + ".wav")
    payloads = [b"first-publisher" * 100, b"second-publisher" * 100]
    staged = threading.Barrier(3, timeout=5)
    release = threading.Event()
    original = Path.write_bytes
    temporary_paths = []

    def write(path, data):
        if ".tmp-" not in path.name:
            return original(path, data)
        temporary_paths.append(path)
        with path.open("wb") as handle:
            handle.write(data[:4])
            handle.flush()
            staged.wait()
            assert release.wait(5), "publication release timeout"
            handle.write(data[4:])
        return len(data)

    monkeypatch.setattr(Path, "write_bytes", write)
    with target.open("rb") as reader, ThreadPoolExecutor(max_workers=2) as pool:
        fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        futures = [pool.submit(publish, tts, kind, data) for data in payloads]
        try:
            staged.wait()
            assert len(set(temporary_paths)) == 2
            assert target.read_bytes() == b"old-audio"
            assert not tts.executor.jobs
            tts.worker._prune_audio_cache(tts.root, 0, 0)
            assert target.read_bytes() == b"old-audio"
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
        assert reader.read() == b"old-audio"
    assert target.read_bytes() in payloads
    assert list(tts.root.iterdir()) == [target]
    assert len(tts.executor.jobs) == 1
    if kind == "direct":
        assert all(result["cache_stored"] for result in results)
    else:
        assert all("standard-cache store " in result[0] for result in results)


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_optional_store_failure_preserves_destination_and_cleans_temporary(tts, monkeypatch, stage):
    target = cache_file(tts, "a" * 64 + ".wav")
    original = Path.write_bytes

    def write(path, data):
        if ".tmp-" in path.name:
            original(path, b"partial")
            raise OSError("controlled write failure")
        return original(path, data)

    if stage == "write":
        monkeypatch.setattr(Path, "write_bytes", write)
    else:
        local_os = SimpleNamespace(**vars(os))
        local_os.replace = lambda *a: (_ for _ in ()).throw(OSError("controlled replace failure"))
        monkeypatch.setattr(tts.worker, "os", local_os)
    logs = publish(tts, "standard", b"new-audio")
    assert "standard-cache store falhou: OSError:" in logs[0]
    assert target.read_bytes() == b"old-audio" and list(tts.root.iterdir()) == [target]
    assert not tts.executor.jobs


def test_touch_is_throttled_bounded_and_observes_live_clock_and_state(tts, monkeypatch):
    path = cache_file(tts, "touch.wav")
    called = []
    local_os = SimpleNamespace(**vars(os))
    local_os.utime = lambda *a: called.append(a)
    monkeypatch.setattr(tts.worker, "os", local_os)
    clock = [100.0]
    tts.worker.time.monotonic = lambda: clock[0]
    tts.handler._touch_tts_cache_file(path)
    clock[0] = 129.99
    tts.handler._touch_tts_cache_file(path)
    assert len(called) == 1
    clock[0] = 130.0
    tts.handler._touch_tts_cache_file(path)
    assert len(called) == 2
    state = {str(i): 0.0 for i in range(4096)}
    monkeypatch.setattr(tts.worker, "_TTS_CACHE_TOUCHES", state)
    tts.handler._touch_tts_cache_file(path)
    assert len(state) == 4096 and "0" not in state and state[str(path)] == 130.0


def test_touch_oserror_is_optional_and_still_throttled(tts, monkeypatch):
    local_os = SimpleNamespace(**vars(os))
    called = []

    def unavailable(*args):
        called.append(args)
        raise OSError("controlled utime failure")

    local_os.utime = unavailable
    monkeypatch.setattr(tts.worker, "os", local_os)
    path = tts.root / "gone.wav"
    tts.handler._touch_tts_cache_file(path)
    tts.handler._touch_tts_cache_file(path)
    assert len(called) == 1


def test_find_skips_unreadable_and_empty_formats_in_existing_order(tts, monkeypatch):
    key = "a" * 64
    mp3 = cache_file(tts, key + ".mp3", b"mp3")
    cache_file(tts, key + ".wav", b"")
    ogg = cache_file(tts, key + ".ogg", b"ogg")
    assert tts.handler._find_tts_cache_file(key) == (mp3, "mp3")
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == mp3:
            raise PermissionError("controlled lookup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert tts.handler._find_tts_cache_file(key) == (ogg, "ogg")


def test_direct_lookup_and_optional_synthesis_keep_distinct_read_errors(tts, monkeypatch):
    key = "a" * 64
    target = cache_file(tts, key + ".wav")
    original = Path.read_bytes

    def read(path):
        if path == target:
            raise OSError("controlled read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(OSError, match="controlled read failure"):
        tts.handler._task_tts_cache_lookup({"cache_key": key})
    assert tts.handler._tts_agent_standard_cache_hit(key=key, engine="gtts", roles=[],
        capabilities=[], logs=[], started=100, max_audio_bytes=1024) is None
