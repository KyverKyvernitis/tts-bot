"""Aquecimento local e corridas com o primeiro consumidor do helper."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente import ytdlp_quente
from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent


def wait_file(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"helper não sinalizou {path.name}")
        time.sleep(0.01)


def blocked_helper(tmp_path: Path):
    started, release, requests = [tmp_path / name for name in ("started", "release", "requests")]
    script = tmp_path / "helper.py"
    script.write_text(
        "import json, sys, time\nfrom pathlib import Path\n"
        f"started=Path({str(started)!r}); release=Path({str(release)!r}); requests=Path({str(requests)!r})\n"
        "for raw in sys.stdin:\n"
        " r=json.loads(raw)\n"
        " with requests.open('a') as log: log.write(json.dumps(r)+'\\n')\n"
        " if r.get('action')=='initialize':\n"
        "  started.touch()\n"
        "  while not release.exists(): time.sleep(.01)\n"
        "  result={'ok':True,'id':r['id'],'ready':True}\n"
        " else: result={'ok':True,'id':r['id'],'stream_url':'https://media.example/audio'}\n"
        " print(json.dumps(result), flush=True)\n",
        encoding="utf-8",
    )
    return ytdlp_quente.WarmYTDLPResolver(script_path=str(script)), started, release, requests


def test_initialize_is_offline_and_reuses_identical_configuration(monkeypatch):
    instances, extractions = [], []

    def parse_options(args):
        result = {"format": args[args.index("-f") + 1],
                  "format_sort": args[args.index("--format-sort") + 1],
                  "socket_timeout": int(args[args.index("--socket-timeout") + 1])}
        return SimpleNamespace(ydl_opts=result)

    class YDL:
        def __init__(self, options):
            self.options = options
            instances.append(self)

        def close(self):
            pass

        def extract_info(self, target, *, download):
            assert download is False
            extractions.append(target)
            return {"title": "Track", "url": "https://media.example/audio", "acodec": "opus",
                    "vcodec": "none", "format_id": "251", "abr": 128, "asr": 48000}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(parse_options=parse_options, YoutubeDL=YDL))
    base = {"format": "bestaudio/best", "format_sort": "abr,acodec,asr", "js_runtimes": "node"}
    commands = [dict(base, id=1, action="initialize"), dict(base, id=2, action="initialize"),
                dict(base, id=3, target="https://youtube.com/watch?v=example"),
                dict(base, id=4, action="initialize", format="140")]
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(item) for item in commands)))
    monkeypatch.setattr(sys, "stdout", stdout)

    assert ytdlp_quente._worker_main() == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4]
    assert replies[0]["ready"] and replies[1]["ready"] and replies[3]["ready"]
    assert extractions == [commands[2]["target"]]
    assert len(instances) == 2  # warmup + resolve reuse; config change rebuilds
    assert instances[0].options["format"] == "bestaudio/best"
    assert instances[1].options["format"] == "140"


def test_real_ytdlp_can_initialize_with_network_disabled(monkeypatch, tmp_path):
    pytest.importorskip("yt_dlp")
    script = tmp_path / "offline.py"
    script.write_text(
        "import socket, runpy\n"
        "def forbidden(*args, **kwargs): raise AssertionError('warmup tentou usar rede')\n"
        "socket.socket.connect=forbidden\nsocket.socket.connect_ex=forbidden\n"
        "socket.getaddrinfo=forbidden\nsocket.create_connection=forbidden\n"
        f"runpy.run_path({str(Path(ytdlp_quente.__file__).resolve())!r},run_name='__main__')\n"
    )
    monkeypatch.setattr(ytdlp_quente.shutil, "which", lambda _name: sys.executable)
    client = ytdlp_quente.WarmYTDLPResolver(script_path=str(script))
    try:
        assert client.prewarm(format_selector="bestaudio/best", timeout=5)
        assert client.state == "warm"
    finally:
        client.close()


def test_first_resolve_shares_running_initialization_and_process(tmp_path):
    client, started, release, requests = blocked_helper(tmp_path)
    warmed, resolved = [], []
    options = {"format_selector": "bestaudio/best", "format_sort": "abr,acodec,asr",
               "cookiefile": "/not-a-secret-fixture", "js_runtimes": "node", "socket_timeout": 12}
    warm = threading.Thread(target=lambda: warmed.append(client.prewarm(**options)))
    play = threading.Thread(target=lambda: resolved.append(client.resolve("chosen", **options, timeout=3)))
    try:
        warm.start()
        wait_file(started)
        pid = client._process.pid
        assert client.state == "warming"
        play.start()
        release.touch()
        warm.join(3)
        play.join(3)
        assert not warm.is_alive() and not play.is_alive()
        assert warmed == [True] and resolved[0]["stream_url"] == "https://media.example/audio"
        assert client._process.pid == pid and client.state == "warm"
        sent = [json.loads(line) for line in requests.read_text().splitlines()]
        assert len(sent) == 2 and sent[0]["action"] == "initialize" and sent[1]["target"] == "chosen"
        assert {key: value for key, value in sent[0].items() if key not in {"id", "action"}} == {
            key: value for key, value in sent[1].items() if key not in {"id", "target"}}
    finally:
        release.touch()
        client.close()
        warm.join(3)
        if play.ident is not None:
            play.join(3)


def test_cancelling_waiter_does_not_cancel_shared_warmup(tmp_path):
    client, started, release, _requests = blocked_helper(tmp_path)
    warmed, errors = [], []
    cancel = threading.Event()
    warm = threading.Thread(target=lambda: warmed.append(client.prewarm(format_selector="bestaudio")))

    def resolve():
        try:
            client.resolve("cancelled", format_selector="bestaudio", cancel_event=cancel)
        except RuntimeError as exc:
            errors.append(str(exc))

    play = threading.Thread(target=resolve)
    try:
        warm.start()
        wait_file(started)
        play.start()
        cancel.set()
        play.join(2)
        assert not play.is_alive() and errors and "cancelada" in errors[0]
        assert client.state == "warming" and client._process.poll() is None
        release.touch()
        warm.join(3)
        assert warmed == [True]
        assert client.resolve("later", format_selector="bestaudio")["stream_url"]
    finally:
        release.touch()
        client.close()
        warm.join(3)
        if play.ident is not None:
            play.join(3)


def test_partial_reply_respects_deadline_and_reaps_process(tmp_path, monkeypatch):
    script = tmp_path / "partial.py"
    script.write_text("import sys,time\nsys.stdin.readline()\nsys.stdout.write('{\"ok\":');sys.stdout.flush()\ntime.sleep(30)\n")
    client = ytdlp_quente.WarmYTDLPResolver(script_path=str(script))
    processes = []
    original = client._ensure_locked

    def capture():
        proc = original()
        processes.append(proc)
        return proc

    monkeypatch.setattr(client, "_ensure_locked", capture)
    started = time.monotonic()
    try:
        assert client.prewarm(format_selector="bestaudio", timeout=0.15) is False
        assert time.monotonic() - started < 2
        assert client._process is None and processes[0].poll() is not None
        assert client.state == "cold"
    finally:
        client.close()


def test_busy_real_resolution_keeps_immediate_fallback(tmp_path):
    client, _started, release, _requests = blocked_helper(tmp_path)
    try:
        assert client._lock.acquire(blocking=False)
        assert client.resolve("busy", format_selector="bestaudio") is None
        assert client.prewarm(format_selector="bestaudio") is False
        assert client._process is None
    finally:
        client._lock.release()
        release.touch()
        client.close()


def test_foreground_timeout_does_not_kill_shared_initialization(tmp_path):
    client, started, release, requests = blocked_helper(tmp_path)
    warmed = []
    warm = threading.Thread(target=lambda: warmed.append(client.prewarm(format_selector="bestaudio")))
    try:
        warm.start()
        wait_file(started)
        assert client.resolve("too-late", format_selector="bestaudio", timeout=0.5) is None
        assert client.state == "warming" and client._process.poll() is None
        assert len(requests.read_text().splitlines()) == 1
        release.touch()
        warm.join(3)
        assert warmed == [True]
        assert client.resolve("later", format_selector="bestaudio")["stream_url"]
    finally:
        release.touch()
        client.close()
        warm.join(3)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_cleanup_kills_owned_group_even_after_parent_exit(monkeypatch):
    client = ytdlp_quente.WarmYTDLPResolver()
    killed = []
    client._process = SimpleNamespace(pid=987654, poll=lambda: 0, stdin=io.StringIO(), stdout=io.StringIO())
    monkeypatch.setattr(ytdlp_quente.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    client.close()
    assert killed == [(987654, ytdlp_quente.signal.SIGKILL)]
    assert client._process is None


@pytest.mark.asyncio
async def test_runtime_prewarm_is_optin_and_excludes_archive_mode(monkeypatch):
    music = _load_music_agent(monkeypatch)
    monkeypatch.delenv("MUSIC_AGENT_YTDLP_PREWARM_ENABLED", raising=False)
    agent = music.MusicAgent()
    agent._start_ytdlp_prewarm()
    assert agent._ytdlp_prewarm_task is None and agent._ytdlp_warm_client is None
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_PREWARM_ENABLED", "true")
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "false")
    agent._start_ytdlp_prewarm()
    assert agent._ytdlp_prewarm_task is None
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    agent.executor_mode = "archive"
    agent._start_ytdlp_prewarm()
    assert agent._ytdlp_prewarm_task is None


@pytest.mark.asyncio
async def test_shutdown_blocks_late_helper_creation_and_cli_fallback(monkeypatch):
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    await agent._shutdown_ytdlp_helper()
    with pytest.raises(RuntimeError, match="cancelada"):
        agent._get_ytdlp_warm_client()
    for enabled in ("true", "false"):
        monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", enabled)
        with pytest.raises(RuntimeError, match="cancelada"):
            agent._resolve_with_ytdlp("https://youtube.com/watch?v=fixture")
    assert agent._ytdlp_warm_client is None


@pytest.mark.asyncio
async def test_shutdown_waits_for_real_cleanup_despite_repeated_cancellation(monkeypatch, tmp_path):
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    client, _started, release, _requests = blocked_helper(tmp_path)
    release.touch()
    assert await asyncio.to_thread(client.prewarm, format_selector="bestaudio", timeout=2)
    process = client._process
    agent._ytdlp_warm_client = client
    closing, finish_close = threading.Event(), threading.Event()
    original = client.close
    calls = []

    def slow_close():
        calls.append(1)
        closing.set()
        finish_close.wait(3)
        original()

    monkeypatch.setattr(client, "close", slow_close)
    task = asyncio.create_task(agent._shutdown_ytdlp_helper())
    try:
        assert await asyncio.to_thread(closing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and agent._ytdlp_warm_client is client
        with pytest.raises(RuntimeError, match="cancelada"):
            agent._get_ytdlp_warm_client()
        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [1]
        assert process.poll() is not None and agent._ytdlp_warm_client is None
    finally:
        finish_close.set()
        await asyncio.gather(task, return_exceptions=True)
        original()


@pytest.mark.asyncio
async def test_cancelled_prewarm_retains_cleanup_ownership(monkeypatch):
    music = _load_music_agent(monkeypatch)
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_PREWARM_ENABLED", "true")
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    monkeypatch.setenv("MUSIC_AGENT_EXECUTOR_MODE", "full")
    agent = music.MusicAgent()
    starting, closing, finish_close, release = [threading.Event() for _ in range(4)]
    close_calls = []

    class Hot:
        def prewarm(self, **kwargs):
            starting.set()
            return release.wait(3)

        def close(self):
            close_calls.append(1)
            closing.set()
            finish_close.wait(3)
            release.set()

    agent._ytdlp_warm_client = Hot()
    agent._start_ytdlp_prewarm()
    task = agent._ytdlp_prewarm_task
    try:
        assert await asyncio.to_thread(starting.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(closing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and close_calls == [1]
        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert release.is_set() and close_calls == [1]
    finally:
        finish_close.set()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_startup_login_and_loop_progress_while_warmup_waits(monkeypatch):
    music = _load_music_agent(monkeypatch)
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_PREWARM_ENABLED", "true")
    monkeypatch.setenv("MUSIC_AGENT_YTDLP_WARM_HELPER_ENABLED", "true")
    monkeypatch.setenv("MUSIC_AGENT_EXECUTOR_MODE", "full")
    agent = music.MusicAgent()
    main_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    login_started = asyncio.Event()
    close_threads = []

    class Hot:
        def prewarm(self, **kwargs):
            assert 0 < kwargs["timeout"] <= 5.0
            assert kwargs["format_selector"] == agent.ytdlp_format
            started.set()
            return release.wait(3)

        def close(self):
            close_threads.append(threading.get_ident())
            release.set()

    class Runner:
        def __init__(self, app):
            pass

        async def setup(self):
            pass

        async def cleanup(self):
            pass

    class Site:
        def __init__(self, *args):
            pass

        async def start(self):
            pass

    async def login(_token):
        login_started.set()
        await asyncio.Future()

    async def maintenance():
        await asyncio.Future()

    agent._ytdlp_warm_client = Hot()
    agent.discord_token = "fixture"
    monkeypatch.setattr(agent.client, "start", login, raising=False)
    monkeypatch.setattr(agent, "_archive_temp_maintenance", maintenance)
    monkeypatch.setattr(music.web, "AppRunner", Runner)
    monkeypatch.setattr(music.web, "TCPSite", Site)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(login_started.wait(), 2)
        assert await asyncio.to_thread(started.wait, 2)
        assert not agent._ytdlp_prewarm_task.done()
        await agent._shutdown_ytdlp_helper()
        assert close_threads and all(thread != main_thread for thread in close_threads)
        assert agent._ytdlp_warm_client is None and agent._ytdlp_prewarm_task is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.shutdown()
