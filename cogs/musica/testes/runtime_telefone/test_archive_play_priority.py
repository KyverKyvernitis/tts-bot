from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp.payload
import pytest

from cogs.musica.runtime_telefone.agente.archive_manifest import file_sha256
from cogs.musica.runtime_telefone.agente.archive_pipeline import ArchivePipelineMixin
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin


class Worker(ArchiveMixin):
    def __init__(self):
        self.states = {}
        self._active_resolve_tasks = {}
        self._archive_foreground_until = {}


def test_foreground_guard_stops_at_music_start_but_keeps_short_frame_reservation():
    worker = Worker()
    worker.states[1] = SimpleNamespace(status="preparing")
    assert worker._archive_foreground_busy()
    worker.states[1].status = "playing"
    assert not worker._archive_foreground_busy()
    worker._archive_foreground_until[1] = time.monotonic() + 1
    assert worker._archive_foreground_busy()
    worker._archive_foreground_until[1] = time.monotonic() - 1
    assert not worker._archive_foreground_busy()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="private POSIX process groups")
async def test_running_archive_pauses_then_resumes_without_consuming_its_timeout(tmp_path):
    worker = Worker()
    progress = tmp_path / "progress"
    code = "import pathlib,time,sys\np=pathlib.Path(sys.argv[1])\nfor i in range(35):\n p.write_text(str(i))\n time.sleep(.01)"
    proc = await worker._archive_spawn(sys.executable, "-c", code, str(progress),
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert os.getpgid(proc.pid) == proc.pid
    assert os.getpriority(os.PRIO_PROCESS, proc.pid) >= 10
    monitor = asyncio.create_task(worker._archive_download_communicate(proc, tmp_path, budget=None, timeout=0.65))
    try:
        while not progress.exists():
            await asyncio.sleep(0.01)
        began = time.monotonic()
        worker._archive_foreground_until[1] = began + 0.55
        await asyncio.sleep(0.15)
        frozen = progress.read_text()
        await asyncio.sleep(0.15)
        assert progress.read_text() == frozen
        assert not monitor.done()
        result = await asyncio.wait_for(monitor, timeout=2)
        assert result == (b"", b"") and proc.returncode == 0
        assert time.monotonic() - began > 0.65
        assert progress.read_text() == "34"
    finally:
        if not monitor.done():
            monitor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await monitor


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="private POSIX process groups")
async def test_cancellation_of_suspended_archive_kills_group_and_preserves_partial_file(tmp_path):
    worker = Worker()
    child_pid = tmp_path / "child"
    partial = tmp_path / "audio.ogg.part"
    child_code = "import time; time.sleep(60)"
    code = ("import subprocess,pathlib,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]])\n"
            "pathlib.Path(sys.argv[2]).write_text(str(child.pid))\n"
            "pathlib.Path(sys.argv[3]).write_bytes(b'resumable')\n"
            "time.sleep(60)")
    proc = await worker._archive_spawn(sys.executable, "-c", code, child_code, str(child_pid), str(partial),
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    monitor = asyncio.create_task(worker._archive_download_communicate(proc, tmp_path, budget=None, timeout=5))
    while not child_pid.exists():
        await asyncio.sleep(0.01)
    worker._archive_foreground_until[1] = time.monotonic() + 5
    await asyncio.sleep(0.15)
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(monitor, timeout=2)
    assert proc.returncode == -9
    assert partial.read_bytes() == b"resumable"
    # The worker can reap its direct child; the group descendant may briefly
    # remain an adopted zombie, but must no longer consume CPU or hold pipes.
    proc_state = Path(f"/proc/{int(child_pid.read_text())}/stat")
    if proc_state.exists():
        assert proc_state.read_text().split(") ", 1)[1].split()[0] == "Z"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="private POSIX process groups")
async def test_archive_gets_fair_work_slices_during_prolonged_voice_recovery(tmp_path, monkeypatch):
    globals_ = ArchiveMixin._archive_download_communicate.__globals__
    monkeypatch.setitem(globals_, "_ARCHIVE_PAUSE_SLICE_SECONDS", 0.12)
    monkeypatch.setitem(globals_, "_ARCHIVE_RUN_SLICE_SECONDS", 0.11)
    worker = Worker()
    progress = tmp_path / "fair-progress"
    code = "import pathlib,time,sys\np=pathlib.Path(sys.argv[1])\nfor i in range(25):\n p.write_text(str(i))\n time.sleep(.01)"
    proc = await worker._archive_spawn(sys.executable, "-c", code, str(progress),
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    worker.states[1] = SimpleNamespace(status="reconnecting")
    result = await asyncio.wait_for(worker._archive_download_communicate(proc, tmp_path, budget=None, timeout=1), timeout=2)
    assert result == (b"", b"") and proc.returncode == 0
    assert progress.read_text() == "24"
    assert worker._archive_foreground_busy()  # recovery may still be pending


def test_process_signals_never_target_an_unowned_group(monkeypatch):
    fake = SimpleNamespace(pid=123, returncode=None, _archive_process_group=True)
    monkeypatch.setattr(os, "getpgid", lambda _: 999)
    def forbidden(*args):
        raise AssertionError("unowned process group")
    monkeypatch.setattr(os, "killpg", forbidden)
    assert not ArchivePipelineMixin._archive_signal_process(fake, 19)


@pytest.mark.asyncio
async def test_upload_pauses_in_aiohttp_executor_without_blocking_event_loop_or_corrupting_bytes(tmp_path):
    worker = Worker()
    worker.states[1] = SimpleNamespace(status="playing")
    path = tmp_path / "part.ogg"
    data = bytes(range(256)) * 1024
    path.write_bytes(data)
    file = worker._archive_upload_file(path, filename="part.ogg")
    payload = aiohttp.payload.IOBasePayload(file.fp)
    worker._archive_foreground_until[1] = time.monotonic() + 0.2
    chunks = []
    class Writer:
        async def write(self, chunk):
            chunks.append(chunk)
    heartbeat = 0
    began = time.monotonic()
    task = asyncio.create_task(payload.write(Writer()))
    try:
        while not task.done():
            heartbeat += 1
            await asyncio.sleep(0.01)
        await task
        assert b"".join(chunks) == data
        assert len(chunks) >= 4 and max(map(len, chunks)) <= 65536
        assert heartbeat >= 15
        assert time.monotonic() - began >= 0.45
        # Discord can seek and resend the exact file after a 429 or transient
        # request failure; no truncated read is mistaken for EOF.
        file.reset()
        assert file.fp.read() == data
    finally:
        file.close()
    assert file.fp.closed


@pytest.mark.asyncio
async def test_archive_file_hash_yields_after_start_and_cancellation_stops_thread(tmp_path):
    worker = Worker()
    path = tmp_path / "audio.ogg"
    data = b"bytes" * 1000
    path.write_bytes(data)
    assert await worker._archive_background_call(file_sha256, path) == hashlib.sha256(data).hexdigest()
    started, busy = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    def work(*, checkpoint):
        loop.call_soon_threadsafe(started.set)
        while not busy.is_set():
            time.sleep(0.005)
        checkpoint()
        return "completed"
    task = asyncio.create_task(worker._archive_background_call(work))
    await started.wait()
    worker._archive_foreground_until[1] = time.monotonic() + 5
    busy.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_legacy_attachment_migration_streams_bounded_chunks_and_commits_complete_file(tmp_path, monkeypatch):
    import aiohttp.web
    from cogs.musica.runtime_telefone.agente import archive_pipeline, validade_stream
    content = bytes(range(256)) * 700
    async def serve(request):
        return aiohttp.web.Response(body=content)
    app = aiohttp.web.Application()
    app.router.add_get("/legacy", serve)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    class Attachment:
        url = f"http://127.0.0.1:{port}/legacy"
        size = len(content)
        async def save(self, *args):
            raise AssertionError("migration must not buffer attachment.save")
    monkeypatch.setattr(archive_pipeline.discord, "Attachment", Attachment)
    monkeypatch.setattr(validade_stream, "valid_cdn_url", lambda value: value)
    worker = Worker()
    chunks = []
    checkpoint = worker._archive_transfer_checkpoint
    async def counted(transfer, *, chunk_bytes):
        chunks.append(chunk_bytes)
        await checkpoint(transfer, chunk_bytes=chunk_bytes)
    worker._archive_transfer_checkpoint = counted
    destination = tmp_path / "legacy.ogg"
    try:
        await worker._archive_download_attachment(Attachment(), destination)
        assert destination.read_bytes() == content
        assert max(chunks) <= 65536 and len(chunks) >= 3
        assert not destination.with_name(destination.name + ".part").exists()
    finally:
        await runner.cleanup()
