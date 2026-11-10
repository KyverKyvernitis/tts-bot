"""Local private-socket contracts; no Discord connection or upstream provider."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def music(monkeypatch):
    # Match the repository's existing isolated runtime test setup.
    from test_tts_helpers import tts_audio
    import discord
    if not hasattr(discord, "AudioSource"):
        monkeypatch.setattr(discord, "AudioSource", type("NativeTestAudioSource", (), {}), raising=False)
    return importlib.import_module("cogs.musica.runtime_telefone.agente.tts")


@contextmanager
def server(tmp_path, respond):
    path = str(tmp_path / "tts.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    listener.settimeout(3)
    received, errors = [], []

    def run():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                raw = b""
                while b"\r\n\r\n" not in raw:
                    raw += connection.recv(1024)
                head, body = raw.split(b"\r\n\r\n", 1)
                length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                while len(body) < length:
                    body += connection.recv(length - len(body))
                received.append(json.loads(body))
                respond(connection)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield path, received
    finally:
        listener.close()
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert not errors, errors


def headers(engine="edge"):
    return ("HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n"
            "Content-Type: audio/mpeg\r\nX-Core-Worker-Audio-Format: mp3\r\n"
            f"X-Core-Worker-Engine: {engine}\r\nX-Core-Worker-Stream-Protocol: 2\r\n"
            "X-Core-Worker-Cache-Hit: false\r\n\r\n").encode()


def test_native_reader_preserves_bytes_and_rejects_partial_or_wrong_engine(music, monkeypatch, tmp_path):
    for index, (engine, tail, valid) in enumerate([
            ("edge", b"3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n", True),
            ("edge", b"3\r\nabc\r\n", False),
            ("gtts", b"3\r\nabc\r\n0\r\n\r\n", False)]):
        directory = tmp_path / str(index)
        directory.mkdir()
        with server(directory, lambda connection: connection.sendall(headers(engine) + tail)) as (path, received):
            monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", path)
            reader = music._NativeTTSReader({"engine": "edge", "text": "olá"}, timeout=2, max_bytes=32)
            chunks = []
            while True:
                chunk = reader.read(2)
                if not chunk:
                    break
                chunks.append(chunk)
            reader.close()
            if valid:
                assert b"".join(chunks) == b"abcdef"
                assert reader.error is None
            else:
                assert reader.error is not None
            assert received[0]["text"] == "olá"


def test_native_reader_close_unblocks_physical_socket_read(music, monkeypatch, tmp_path):
    disconnected = threading.Event()

    def respond(connection):
        connection.sendall(headers() + b"3\r\nabc\r\n")
        if connection.recv(1) == b"":
            disconnected.set()

    with server(tmp_path, respond) as (path, received):
        monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", path)
        reader = music._NativeTTSReader({"engine": "edge", "text": "olá"}, timeout=2, max_bytes=32)
        assert reader.read(3) == b"abc"
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(reader.read, 3)
            time.sleep(0.02)
            reader.close()
            assert reading.result(timeout=1) == b""
        assert disconnected.wait(1)


@pytest.mark.asyncio
async def test_native_file_branch_never_imports_python_provider(music, monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    original = music.importlib.import_module

    def guarded_import(name, *args, **kwargs):
        if name in {"gtts", "edge_tts", "tts_transport"}:
            raise AssertionError("native synthesis imported Python provider")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(music.importlib, "import_module", guarded_import)

    class Owner(music.TTSMixin):
        def _tts_cache_enabled(self):
            return False

    with server(tmp_path, lambda connection: connection.sendall(headers() + b"3\r\nabc\r\n0\r\n\r\n")) as (path, received):
        monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", path)
        target = tmp_path / "speech.mp3"
        body = {"engine": "edge_tts", "text": "olá"}
        assert await Owner()._synthesize_tts_file(body, target) == "edge"
        assert target.read_bytes() == b"abc"
        assert body["audio_format"] == "mp3"
        assert received[0]["cache_mode"] == "bypass"
    with pytest.raises(ValueError, match="piper indisponível"):
        await Owner()._synthesize_tts_file({"engine": "piper", "text": "olá"}, tmp_path / "unsupported.mp3")


@pytest.mark.asyncio
async def test_native_decoder_branch_keeps_existing_effects_and_mixer_boundary(music, monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", str(tmp_path / "unused.sock"))
    calls = []
    fake_reader = SimpleNamespace(engine="gtts", audio_format="mp3", cache_hit=False, close=lambda: None)

    class Owner(music.TTSMixin):
        ffmpeg_executable = "ffmpeg"

        def _tts_cache_enabled(self):
            return False

        def _native_tts_reader(self, body, **kwargs):
            calls.append(kwargs)
            return fake_reader

        async def _buffer_tts_source(self, source, **kwargs):
            assert kwargs["reader"] is fake_reader
            return source

    def decoder(source, **kwargs):
        assert source is fake_reader
        assert kwargs["pipe"] is True
        assert "-f mp3" in kwargs["before_options"]
        assert "-af" in kwargs["options"]
        return SimpleNamespace(cleanup=lambda: None)

    monkeypatch.setattr(music.discord, "FFmpegPCMAudio", decoder)
    monkeypatch.setattr(music.importlib, "import_module", lambda *a, **k: pytest.fail("native decoder imported provider"))
    source, engine = await Owner()._prepare_tts_source(
        {"engine": "gtts", "text": "olá", "advanced_reverb_level": 1},
        tmp_path / "speech.mp3", started=time.monotonic())
    assert engine == "gtts"
    assert calls[0]["engine"] == "gtts"


@pytest.mark.asyncio
async def test_native_file_cancel_joins_reader_and_removes_staging(music, monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_NATIVE_WORKER", "1")
    started, disconnected = threading.Event(), threading.Event()

    def respond(connection):
        connection.sendall(headers() + b"3\r\nabc\r\n")
        started.set()
        if connection.recv(1) == b"":
            disconnected.set()

    class Owner(music.TTSMixin):
        def _tts_cache_enabled(self):
            return False

    with server(tmp_path, respond) as (path, received):
        monkeypatch.setenv("TTS_NATIVE_WORKER_SOCKET", path)
        target = tmp_path / "speech.mp3"
        task = asyncio.create_task(Owner()._synthesize_tts_file({"engine": "edge", "text": "olá"}, target))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert disconnected.wait(1)
        assert not target.exists()
        assert not list(tmp_path.glob("speech.mp3.native-*"))


def test_progressive_failure_after_pcm_is_visible(music):
    class Source:
        def __init__(self):
            self.frames = iter([b"pcm", b""])

        def read(self):
            return next(self.frames)

        def cleanup(self):
            pass

    reader = SimpleNamespace(error=RuntimeError("incomplete native chunked stream"), close=lambda: None)
    timed = music._TimedTTSSource(Source(), started=time.monotonic(), reader=reader, buffered=True)
    assert timed.ready.wait(1)
    assert timed.read() == b"pcm"
    deadline = time.monotonic() + 1
    while not timed.finished and time.monotonic() < deadline:
        time.sleep(0.001)
    with pytest.raises(RuntimeError, match="síntese progressiva incompleta"):
        timed.read()
    timed.cleanup()
