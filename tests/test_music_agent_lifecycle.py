import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MUSIC = ROOT / "deploy/termux/phone-worker/music_agent.py"


def _load_music_agent(monkeypatch):
    discord = types.ModuleType("discord")
    class AudioSource: pass
    class VoiceClient: pass
    class FFmpegPCMAudio: pass
    class PCMVolumeTransformer:
        def __init__(self, source, volume=1.0): self.source, self.volume = source, volume
    class Intents:
        @classmethod
        def none(cls):
            obj = cls(); obj.guilds = False; obj.voice_states = False; return obj
    class Client:
        def __init__(self, *, intents=None): self.intents=intents; self.user=None
        def event(self, fn): setattr(self, fn.__name__, fn); return fn
        def get_guild(self, _): return None
        def get_channel(self, _): return None
        def is_ready(self): return False
    discord.AudioSource=AudioSource; discord.VoiceClient=VoiceClient; discord.FFmpegPCMAudio=FFmpegPCMAudio
    discord.PCMVolumeTransformer=PCMVolumeTransformer; discord.Intents=Intents; discord.Client=Client


    aiohttp = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")
    class Application:
        def __init__(self): self.routes=[]
        def add_routes(self, routes): self.routes.extend(routes)
    class Request: pass
    class Response: pass
    class AppRunner: pass
    class TCPSite: pass
    web.Application=Application; web.Request=Request; web.Response=Response; web.AppRunner=AppRunner; web.TCPSite=TCPSite
    web.get=lambda *a, **k: ("get", a, k); web.post=lambda *a, **k: ("post", a, k)
    web.json_response=lambda data, status=200: (data, status)
    aiohttp.web=web

    monkeypatch.syspath_prepend(str(MUSIC.parent))
    monkeypatch.setitem(sys.modules, "discord", discord)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)
    name = "music_agent_lifecycle_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MUSIC)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def music(monkeypatch):
    return _load_music_agent(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def test_idle_cancelled_task_cannot_remove_replacement(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent(); gate = asyncio.Event(); gid = 10
        real_sleep = asyncio.sleep
        async def fake_sleep(_): await gate.wait()
        monkeypatch.setattr(music.asyncio, "sleep", fake_sleep)
        old = asyncio.create_task(agent._idle_disconnect_later(gid, 99))
        agent._idle_disconnect_tasks[gid] = old
        await real_sleep(0)
        old.cancel()
        replacement = asyncio.create_task(real_sleep(999))
        agent._idle_disconnect_tasks[gid] = replacement
        await old
        assert agent._idle_disconnect_tasks.get(gid) is replacement
        replacement.cancel()
    run(scenario())


def test_prefetch_cancelled_task_cannot_remove_replacement(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent(); gid=11; key="song"; task_key=agent._guild_prefetch_key(gid,key)
        gate=asyncio.Event()
        async def resolve(*a, **k): await gate.wait()
        agent.resolve_track=resolve
        old=asyncio.create_task(agent._prefetch_track({"guild_id":gid},{"title":"x"},"x",key))
        agent._prefetch_tasks[task_key]=old
        await asyncio.sleep(0)
        old.cancel()
        replacement=asyncio.create_task(asyncio.sleep(999))
        agent._prefetch_tasks[task_key]=replacement
        await old
        assert agent._prefetch_tasks.get(task_key) is replacement
        replacement.cancel()
    run(scenario())


def test_stop_failure_still_attempts_disconnect_direct(music):
    class Player:
        def __init__(self): self.disconnected=False
        def is_playing(self): return True
        def is_paused(self): return False
        def stop(self): raise RuntimeError("stop")
        async def disconnect(self, force=False): self.disconnected=True
    async def scenario():
        agent=music.MusicAgent(); p=Player(); await agent._stop_player_instance(p, disconnect=True); assert p.disconnected
    run(scenario())


def test_music_agent_source_has_no_wavelink_or_lavalink_runtime_dependency():
    source = MUSIC.read_text(encoding="utf-8")
    for marker in (
        "import wavelink",
        "ensure_lavalink_pool",
        "_play_lavalink",
        "MUSIC_AGENT_LAVALINK",
        "LAVALINK_URI",
        "wavelink.Player",
    ):
        assert marker not in source



def test_cmd_stop_clears_player_before_disconnect_await(music):
    async def scenario():
        agent=music.MusicAgent(); gid=12; st=music.GuildMusicState(guild_id=gid)
        seen=[]
        class Player:
            def is_playing(self): return False
            def is_paused(self): return False
            async def disconnect(self, force=False): seen.append(st.player)
        st.player=Player(); agent.states[gid]=st
        await agent.cmd_stop({"guild_id":gid})
        assert seen == [None]
        assert st.player is None
    run(scenario())


def test_repeated_direct_callback_does_not_advance_preparing_next_track(music):
    async def scenario():
        agent=music.MusicAgent(); gid=13; st=music.GuildMusicState(guild_id=gid)
        st.current=music.AgentTrack(title="one", query="one"); st.queue=[music.AgentTrack(title="two", query="two"), music.AgentTrack(title="three", query="three")]
        st.playback_token=7; st.started_monotonic=music.time.monotonic()-10; agent.states[gid]=st
        entered=asyncio.Event(); release=asyncio.Event()
        async def resolve(query, **kwargs): entered.set(); await release.wait(); return music.AgentTrack(title="two", query="two", stream_url="https://x")
        async def play_direct(gid, track): return None
        agent.resolve_track=resolve; agent._play_direct_voice=play_direct; agent._should_use_direct_voice=lambda track: True
        first=asyncio.create_task(agent._direct_after(gid, None, 7))
        await entered.wait()
        await agent._direct_after(gid, None, 7)
        assert st.current.title == "two"
        assert [t.title for t in st.queue] == ["three"]
        release.set(); await first
    run(scenario())


def test_cancel_prefetch_only_targets_requested_guild(music):
    async def scenario():
        agent=music.MusicAgent(); a=asyncio.create_task(asyncio.sleep(999)); b=asyncio.create_task(asyncio.sleep(999))
        agent._prefetch_tasks={"1:a":a,"2:b":b}; assert agent._cancel_prefetch_tasks(1)==1; assert "2:b" in agent._prefetch_tasks
        b.cancel(); await asyncio.gather(a,b,return_exceptions=True)
    run(scenario())


def test_cancel_prefetch_can_preserve_selected_candidate(music):
    async def scenario():
        agent=music.MusicAgent()
        selected=asyncio.create_task(asyncio.sleep(999))
        other=asyncio.create_task(asyncio.sleep(999))
        agent._prefetch_tasks={"1:selected":selected,"1:other":other}
        assert agent._cancel_prefetch_tasks(1, keep_task_keys={"1:selected"}) == 1
        await asyncio.sleep(0)
        assert agent._prefetch_tasks.get("1:selected") is selected
        assert "1:other" not in agent._prefetch_tasks and other.cancelled()
        selected.cancel()
        await asyncio.gather(selected, other, return_exceptions=True)
    run(scenario())


def test_cmd_play_prunes_unselected_search_prefetch(music):
    async def scenario():
        agent=music.MusicAgent(); gid=21
        selected_meta={"title":"selected","webpage_url":"https://youtu.be/selected"}
        selected_key=agent._guild_prefetch_key(gid, agent._resolve_cache_key("https://youtu.be/selected", selected_meta))
        other_key=agent._guild_prefetch_key(gid, "https://youtu.be/other")
        selected=asyncio.create_task(asyncio.sleep(999))
        other=asyncio.create_task(asyncio.sleep(999))
        agent._prefetch_tasks={selected_key:selected, other_key:other}
        st=music.GuildMusicState(guild_id=gid, status="playing", current=music.AgentTrack(title="current", query="current"))
        agent.states[gid]=st
        async def resolve(query, **kwargs):
            return music.AgentTrack(title="selected", query=query, webpage_url=query, stream_url="https://media.example/audio")
        agent.resolve_track=resolve
        agent._schedule_next_queue_prefetch=lambda *a, **kw: None
        result=await agent.cmd_play({
            "guild_id":gid, "voice_channel_id":99, "text_channel_id":100,
            "query":"https://youtu.be/selected", "track":selected_meta,
        })
        await asyncio.sleep(0)
        assert result["queued"] is True
        assert agent._prefetch_tasks.get(selected_key) is selected
        assert other_key not in agent._prefetch_tasks and other.cancelled()
        selected.cancel()
        await asyncio.gather(selected, other, return_exceptions=True)
    run(scenario())


def test_bump_generation_increments_and_cancels_prefetch(music):
    agent=music.MusicAgent(); st=music.GuildMusicState(guild_id=1); calls=[]; agent._cancel_prefetch_tasks=lambda gid: calls.append(gid) or 0
    assert agent._bump_playback_generation(st, reason="test") == 1; assert calls == [1]


def test_finish_empty_queue_schedules_idle_disconnect(music):
    async def scenario():
        agent=music.MusicAgent(); gid=14; st=music.GuildMusicState(guild_id=gid); st.current=music.AgentTrack(title="one", query="one"); agent.states[gid]=st
        called=[]; agent._schedule_idle_disconnect=lambda x: called.append(x)
        await agent._finish_current(gid,error=None,event="done"); assert st.status=="idle" and called==[gid]
    run(scenario())


def test_cmd_stop_clears_queue_history_and_current(music):
    async def scenario():
        agent=music.MusicAgent(); gid=15; st=music.GuildMusicState(guild_id=gid); st.current=music.AgentTrack(title="one",query="one"); st.queue=[music.AgentTrack(title="two",query="two")]; st.history=[music.AgentTrack(title="old",query="old")]; agent.states[gid]=st
        await agent.cmd_stop({"guild_id":gid}); assert st.current is None and st.queue==[] and st.history==[] and st.status=="idle"
    run(scenario())


def test_stop_without_player_is_idempotent(music):
    async def scenario():
        agent=music.MusicAgent(); result=await agent.cmd_stop({"guild_id":16}); assert result["ok"] is True; result2=await agent.cmd_stop({"guild_id":16}); assert result2["ok"] is True
    run(scenario())


def test_lifecycle_module_remove_requires_same_owner():
    import importlib.util
    path = ROOT / "deploy/termux/phone-worker/music_agent_runtime/lifecycle.py"
    spec = importlib.util.spec_from_file_location("music_agent_runtime_lifecycle_unit", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    first, replacement = object(), object(); registry={"key": replacement}
    assert mod.remove_owned_task(registry, "key", first) is False
    assert registry["key"] is replacement
    assert mod.remove_owned_task(registry, "key", replacement) is True
    assert "key" not in registry


def test_phone_worker_music_dependencies_do_not_require_wavelink():
    source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    block = source.split("def _music_voice_dependency_specs()", 1)[1].split("\n\ndef ", 1)[0]
    assert '"wavelink"' not in block
    assert '"yt-dlp"' in block


def test_phone_worker_music_health_treats_tts_providers_as_optional():
    source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    block = source.split("def _music_voice_dependency_specs()", 1)[1].split("\n\ndef ", 1)[0]
    assert '"gTTS": {"module": "gtts", "pip": "gTTS==2.5.4", "optional": True}' in block
    assert '"edge-tts": {"module": "edge_tts", "pip": "edge-tts==7.2.8", "optional": True}' in block


def test_music_agent_safe_installer_uses_lightweight_ytdlp_package():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-music-agent.sh").read_text(encoding="utf-8")
    assert 'safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp" light' in source
    assert 'yt-dlp[default]' not in source


def test_music_agent_autostart_is_not_turbo_only():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    assert "ensure_music_agent_for_turbo_if_needed" not in source
    assert "ensure_music_agent_if_needed()" in source
    maintenance = source.split("run_post_start_maintenance_async()", 1)[1].split("\n}\n", 1)[0]
    assert "ensure_music_agent_if_needed" in maintenance
    assert "is_turbo_profile || return 0" not in maintenance


def test_music_agent_supervisor_runs_agent_from_active_release():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-music-agent.sh").read_text(encoding="utf-8")
    assert 'RUNTIME_DIR="${PHONE_WORKER_RELEASE_DIR:-$WORKER_DIR}"' in source
    assert 'AGENT_FILE="$RUNTIME_DIR/music_agent.py"' in source
    assert 'cd "$RUNTIME_DIR" || exit 1' in source
    assert 'exec "$PYTHON_BIN" music_agent.py' in source


def test_phone_worker_autostart_prefers_active_release_music_supervisor():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    assert "active_music_agent_start_command()" in source
    assert '$release/start-phone-music-agent.sh' in source
    assert 'PHONE_WORKER_RELEASE_DIR="$release"' in source
    assert 'MUSIC_AGENT_ENV="$MUSIC_AGENT_ENV_FILE"' in source


def test_phone_worker_music_dependency_bootstrap_is_profile_independent_and_lightweight():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    ytdlp = source.split("ensure_music_ytdlp_deps_if_needed()", 1)[1].split("\n}\n", 1)[0]
    music_deps = source.split("ensure_music_agent_deps_if_needed()", 1)[1].split("\n}\n", 1)[0]
    assert "is_turbo_profile || return 0" not in ytdlp
    assert "is_turbo_profile || return 0" not in music_deps
    assert '"yt-dlp" "yt_dlp" "yt-dlp" light' in ytdlp
    assert 'yt-dlp[default]' not in ytdlp + music_deps


def test_phone_worker_duplicate_detection_has_proc_cwd_fallback():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    block = source.split("pid_is_official_worker()", 1)[1].split("\n}\n", 1)[0]
    assert 'if [[ -n "$cwd" ]]' in block
    assert 'phone_worker.py --host' in block
    assert '/proc/<pid>/cwd' in block


def test_music_agent_tts_missing_provider_fails_before_ffmpeg_pipe():
    source = MUSIC.read_text(encoding="utf-8")
    block = source.split("async def _prepare_tts_source", 1)[1].split("\n    async def cmd_tts", 1)[0]
    assert "provider_module = {'gtts': 'gtts', 'edge': 'edge_tts'}.get(engine)" in block
    assert "provider TTS {engine} indisponível no Music Agent; envie áudio pré-sintetizado" in block
    assert block.index("importlib.import_module(provider_module)") < block.index("discord.FFmpegPCMAudio(reader, pipe=True")


def test_phone_worker_music_status_forwards_compact_guild_query():
    source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    block = source.split("def _task_music_agent_proxy", 1)[1].split("\n    def ", 1)[0]
    assert 'guild_id = int(body.get("guild_id") or 0)' in block
    assert '"compact": "1" if compact else "0"' in block
    assert 'known_revision = str(body.get("known_revision") or "").strip()' in block
    assert 'params["known_revision"] = known_revision' in block
    assert 'urllib.parse.urlencode' in block



def test_direct_seek_offset_is_not_counted_twice_in_reported_position(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 31
        channel_id = 901
        track = music.AgentTrack(
            title="seeked",
            query="seeked",
            stream_url="https://media.example/audio",
            duration=195.0,
            start_offset_seconds=60.0,
        )
        st = music.GuildMusicState(
            guild_id=gid,
            voice_channel_id=channel_id,
            current=track,
            status="starting",
        )
        agent.states[gid] = st

        class VoiceClient:
            def __init__(self):
                self.channel = types.SimpleNamespace(id=channel_id)
                self.playing = False
                self.source = None
            def is_connected(self): return True
            def is_playing(self): return self.playing
            def is_paused(self): return False
            def play(self, source, after=None):
                self.source = source
                self.playing = True
            def stop(self): self.playing = False
            async def move_to(self, channel): self.channel = channel

        voice = VoiceClient()
        guild = types.SimpleNamespace(voice_client=voice)
        channel = types.SimpleNamespace(id=channel_id)

        async def resolve_channel(*args, **kwargs):
            return guild, channel
        async def no_sleep(_seconds):
            return None

        agent._loop = asyncio.get_running_loop()
        agent._resolve_guild_and_channel = resolve_channel
        agent._build_ffmpeg_source = lambda *args, **kwargs: object()
        agent._schedule_next_queue_prefetch = lambda *args, **kwargs: None
        monkeypatch.setattr(music.asyncio, "sleep", no_sleep)

        await agent._play_direct_voice(gid, track)
        position = st.public()["position_ms"]
        assert 59_000 <= position <= 61_500

    run(scenario())


def test_pause_freezes_position_and_resume_restarts_prefetch_clock(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 32
        now = [110.0]
        monkeypatch.setattr(music.time, "monotonic", lambda: now[0])

        class Player:
            def __init__(self): self.paused = False
            def pause(self): self.paused = True
            def resume(self): self.paused = False

        st = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="clock", query="clock", start_offset_seconds=10.0),
            status="playing",
            player=Player(),
            started_monotonic=100.0,
        )
        agent.states[gid] = st
        cancelled = []
        scheduled = []
        agent._cancel_prefetch_tasks = lambda value: cancelled.append(value) or 0
        agent._schedule_next_queue_prefetch = lambda value, **kwargs: scheduled.append((value, kwargs.get("reason")))

        await agent.cmd_pause({"guild_id": gid})
        assert st.public()["position_ms"] == 20_000
        now[0] = 210.0
        assert st.public()["position_ms"] == 20_000

        await agent.cmd_resume({"guild_id": gid})
        assert st.started_monotonic == 200.0
        assert st.public()["position_ms"] == 20_000
        now[0] = 215.0
        assert st.public()["position_ms"] == 25_000
        assert cancelled == [gid]
        assert scheduled == [(gid, "resume")]

    run(scenario())


def test_seek_invalidates_prefetch_generation_before_restarting_player(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 33
        stopped = []
        cancelled = []

        class Player:
            def is_playing(self): return True
            def is_paused(self): return False
            def stop(self): stopped.append(True)

        track = music.AgentTrack(
            title="seek",
            query="seek",
            stream_url="https://media.example/audio",
            duration=180.0,
        )
        st = music.GuildMusicState(guild_id=gid, current=track, player=Player(), status="playing", playback_token=5)
        agent.states[gid] = st
        agent._cancel_prefetch_tasks = lambda value: cancelled.append(value) or 0
        async def fake_play(_gid, _track):
            return None
        agent._play_direct_voice = fake_play

        result = await agent.cmd_seek({"guild_id": gid, "position_seconds": 42})
        assert result["ok"] is True
        assert st.playback_token == 6
        assert st.current.start_offset_seconds == 42
        assert cancelled == [gid]
        assert stopped == [True]

    run(scenario())


def test_short_expected_track_end_is_not_misclassified_as_failure(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 34
        st = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="short", query="short", duration=1.0),
            status="playing",
            playback_token=9,
            started_monotonic=music.time.monotonic() - 1.0,
        )
        agent.states[gid] = st
        scheduled = []
        agent._schedule_idle_disconnect = lambda value: scheduled.append(value)

        await agent._direct_after(gid, None, 9)
        assert st.status == "idle"
        assert st.last_error == ""
        assert st.current is None
        assert scheduled == [gid]

    run(scenario())


def test_cmd_play_queues_metadata_without_resolving_when_already_playing(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 31
        st = music.GuildMusicState(
            guild_id=gid,
            status="playing",
            current=music.AgentTrack(title="current", query="current", stream_url="https://media.example/current"),
        )
        agent.states[gid] = st
        calls = []

        async def forbidden_resolve(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("faixa enfileirada não deve resolver yt-dlp imediatamente")

        agent.resolve_track = forbidden_resolve
        agent._schedule_next_queue_prefetch = lambda *args, **kwargs: None
        result = await agent.cmd_play(
            {
                "guild_id": gid,
                "voice_channel_id": 900,
                "text_channel_id": 901,
                "query": "https://youtu.be/next",
                "track": {
                    "title": "next",
                    "webpage_url": "https://youtu.be/next",
                    "duration": 123,
                },
            }
        )

        assert result["ok"] is True
        assert result["queued"] is True
        assert calls == []
        assert len(st.queue) == 1
        assert st.queue[0].title == "next"
        assert st.queue[0].stream_url == ""
        assert result["track"]["title"] == "next"

    run(scenario())


def test_lazy_play_overlaps_voice_preconnect_with_stream_resolution(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 32
        st = music.GuildMusicState(guild_id=gid, voice_channel_id=902)
        st.queue = [music.AgentTrack(title="next", query="next")]
        agent.states[gid] = st
        voice_started = asyncio.Event()
        resolve_started = asyncio.Event()
        release = asyncio.Event()
        voice_client = object()
        seen_prepared = []

        async def ensure_voice(_guild_id):
            voice_started.set()
            await resolve_started.wait()
            await release.wait()
            return voice_client, False

        async def resolve(query, **kwargs):
            resolve_started.set()
            await voice_started.wait()
            await release.wait()
            return music.AgentTrack(title="next", query=query, stream_url="https://media.example/next")

        async def play_direct(_guild_id, track, *, prepared_voice=None):
            seen_prepared.append((track.stream_url, prepared_voice))

        agent._ensure_direct_voice_client = ensure_voice
        agent.resolve_track = resolve
        agent._play_direct_voice = play_direct

        task = asyncio.create_task(agent._play_next(gid))
        await asyncio.wait_for(voice_started.wait(), timeout=1.0)
        await asyncio.wait_for(resolve_started.wait(), timeout=1.0)
        assert not task.done()
        release.set()
        await task

        assert seen_prepared == [("https://media.example/next", (voice_client, False))]
        assert st.current is not None
        assert st.current.stream_url == "https://media.example/next"

    run(scenario())


def test_failed_lazy_resolve_discards_new_preconnected_voice(music):
    class VoiceClient:
        def __init__(self):
            self.disconnected = False
        def is_connected(self):
            return not self.disconnected
        async def disconnect(self, force=False):
            self.disconnected = True

    async def scenario():
        agent = music.MusicAgent()
        gid = 33
        st = music.GuildMusicState(guild_id=gid, voice_channel_id=903)
        st.queue = [music.AgentTrack(title="broken", query="broken")]
        agent.states[gid] = st
        voice_client = VoiceClient()
        voice_ready = asyncio.Event()

        async def ensure_voice(_guild_id):
            voice_ready.set()
            return voice_client, True

        async def resolve(*args, **kwargs):
            await voice_ready.wait()
            await asyncio.sleep(0)
            raise RuntimeError("resolve failed")

        agent._ensure_direct_voice_client = ensure_voice
        agent.resolve_track = resolve
        await agent._play_next(gid)

        assert st.status == "failed"
        assert voice_client.disconnected is True
        assert st.player is None

    run(scenario())


def test_stop_cancels_active_lazy_resolution_without_marking_failure(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 34
        st = music.GuildMusicState(guild_id=gid)
        st.queue = [music.AgentTrack(title="pending", query="pending")]
        agent.states[gid] = st
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def resolve(*args, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        agent.resolve_track = resolve
        play_task = asyncio.create_task(agent._play_next(gid))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert gid in agent._active_resolve_tasks

        result = await agent.cmd_stop({"guild_id": gid})
        await play_task

        assert result["ok"] is True
        assert cancelled.is_set()
        assert gid not in agent._active_resolve_tasks
        assert st.status == "idle"
        assert st.current is None
        assert st.queue == []

    run(scenario())


def test_mixer_records_first_frame_timestamp(music):
    class Source:
        def __init__(self):
            self.frames = [b"\x01\x00" * 1920, b""]
        def read(self):
            return self.frames.pop(0)
        def cleanup(self):
            return None

    async def scenario():
        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Source(),
            music_volume=1.0,
        )
        assert mixer.first_frame_ms is None
        assert mixer.first_frame_monotonic is None
        frame = mixer.read()
        assert frame
        assert mixer.first_frame_ms is not None
        assert mixer.first_frame_ms >= 0.0
        assert mixer.first_frame_monotonic is not None

    run(scenario())


def test_direct_confirmation_returns_immediately_after_first_frame(music, monkeypatch):
    class VoiceClient:
        def is_connected(self): return True
        def is_playing(self): return True
        def is_paused(self): return False

    class Source:
        first_frame_ms = 4.0

    async def scenario():
        agent = music.MusicAgent()
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(music.asyncio, "sleep", fake_sleep)
        elapsed = await agent._confirm_direct_playback(VoiceClient(), Source(), max_delay=0.35)
        assert elapsed >= 0.0
        assert sleeps == []

    run(scenario())


def test_stream_recovery_reresolves_and_resumes_near_last_position(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 41
        agent.stream_recovery_enabled = True
        agent.stream_recovery_max_attempts = 1
        agent.stream_recovery_backtrack_seconds = 0.35
        original = music.AgentTrack(
            title="recover",
            query="https://youtu.be/recover",
            webpage_url="https://youtu.be/recover",
            stream_url="https://expired.example/audio",
            duration=180.0,
            start_offset_seconds=30.0,
        )
        st = music.GuildMusicState(
            guild_id=gid,
            voice_channel_id=900,
            text_channel_id=901,
            current=original,
            status="playing",
        )
        agent.states[gid] = st
        resolved = []
        played = []

        async def fake_resolve(query, *, track_meta, body, priority=0):
            resolved.append((query, body["position_seconds"], priority))
            return music.AgentTrack(
                title="recover",
                query=query,
                webpage_url="https://youtu.be/recover",
                stream_url="https://fresh.example/audio",
                duration=180.0,
            )

        async def fake_play(value, track, **kwargs):
            played.append((value, track.stream_url, track.start_offset_seconds, track.stream_recovery_attempts))

        agent.resolve_track = fake_resolve
        agent._play_direct_voice = fake_play
        assert await agent._recover_current_stream(gid, played_for=10.0, reason="test") is True
        assert resolved == [("https://youtu.be/recover", pytest.approx(39.65), -20)]
        assert played == [(gid, "https://fresh.example/audio", pytest.approx(39.65), 1)]
        assert st.current is not None
        assert st.current.stream_recovery_attempts == 1
        assert st.current.start_offset_seconds == pytest.approx(39.65)

        # Uma segunda falha na mesma execução não pode criar retry infinito.
        assert await agent._recover_current_stream(gid, played_for=5.0, reason="again") is False
        assert len(resolved) == 1

    run(scenario())


def test_direct_after_error_recovers_before_advancing_queue(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 42
        token = 7
        st = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="current", query="current", duration=200.0),
            queue=[music.AgentTrack(title="next", query="next")],
            status="playing",
            playback_token=token,
            started_monotonic=music.time.monotonic() - 12.0,
        )
        agent.states[gid] = st
        recovered = []
        advanced = []

        async def fake_recover(value, *, played_for, reason):
            recovered.append((value, played_for, reason))
            return True

        async def forbidden_next(value, **kwargs):
            advanced.append(value)

        agent._recover_current_stream = fake_recover
        agent._play_next = forbidden_next
        await agent._direct_after(gid, RuntimeError("ffmpeg died"), token)
        assert len(recovered) == 1
        assert recovered[0][0] == gid
        assert recovered[0][1] >= 11.0
        assert recovered[0][2] == "direct_after_error"
        assert advanced == []
        assert st.current.title == "current"
        assert [item.title for item in st.queue] == ["next"]

    run(scenario())


def test_direct_early_end_recovers_before_marking_failed(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 43
        token = 9
        st = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="current", query="current", duration=180.0),
            status="playing",
            playback_token=token,
            started_monotonic=music.time.monotonic() - 0.8,
        )
        agent.states[gid] = st
        reasons = []

        async def fake_recover(value, *, played_for, reason):
            reasons.append(reason)
            return True

        agent._recover_current_stream = fake_recover
        await agent._direct_after(gid, None, token)
        assert reasons == ["direct_after_early_end"]
        assert st.status != "failed"
        assert st.current is not None

    run(scenario())


def test_stream_recovery_counter_is_internal_not_public_contract(music):
    track = music.AgentTrack(title="x", query="x", stream_recovery_attempts=1)
    assert "stream_recovery_attempts" not in track.public()


def test_stale_prefetched_stream_is_reresolved_before_play(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 44
        now = [300.0]
        monkeypatch.setattr(music.time, "monotonic", lambda: now[0])
        agent.stream_refresh_before_play_seconds = 120.0
        stale = music.AgentTrack(
            title="stale",
            query="https://youtu.be/stale",
            webpage_url="https://youtu.be/stale",
            stream_url="https://old.example/audio",
            duration=180.0,
            stream_resolved_monotonic=100.0,
        )
        st = music.GuildMusicState(guild_id=gid, queue=[stale], status="idle")
        agent.states[gid] = st
        resolved_calls = []
        played = []

        async def fake_resolve(query, *, track_meta, body, priority=0):
            resolved_calls.append((query, track_meta.get("title")))
            return music.AgentTrack(
                title="stale",
                query=query,
                webpage_url="https://youtu.be/stale",
                stream_url="https://fresh.example/audio",
                duration=180.0,
                stream_resolved_monotonic=300.0,
            )

        async def fake_play(value, track, **kwargs):
            played.append(track.stream_url)

        agent.resolve_track = fake_resolve
        agent._play_direct_voice = fake_play
        agent._should_use_direct_voice = lambda track: True
        await agent._play_next(gid)
        assert resolved_calls == [("https://youtu.be/stale", "stale")]
        assert played == ["https://fresh.example/audio"]
        assert st.current is not None
        assert st.current.stream_url == "https://fresh.example/audio"

    run(scenario())


def test_direct_start_confirmation_cannot_overwrite_callback_transition(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 45
        channel_id = 904
        track = music.AgentTrack(title="race", query="race", stream_url="https://media.example/race")
        st = music.GuildMusicState(guild_id=gid, voice_channel_id=channel_id, current=track)
        agent.states[gid] = st

        class Voice:
            def __init__(self):
                self.channel = types.SimpleNamespace(id=channel_id)
                self.playing = False
            def is_connected(self): return True
            def is_playing(self): return self.playing
            def is_paused(self): return False
            def stop(self): self.playing = False
            def play(self, source, after=None): self.playing = True

        voice = Voice()
        agent._build_ffmpeg_source = lambda *args, **kwargs: object()

        async def superseded_confirm(*args, **kwargs):
            st.playback_token += 1
            st.status = "preparing"
            raise RuntimeError("old startup lost the race")

        agent._confirm_direct_playback = superseded_confirm
        await agent._play_direct_voice(gid, track, prepared_voice=(voice, False))
        assert st.status == "preparing"
        assert st.playback_token == 2

    run(scenario())


def test_maintenance_prunes_only_expired_idle_state_and_caches(music):
    agent = music.MusicAgent()
    agent.maintenance_interval_seconds = 0.0
    agent.state_idle_ttl_seconds = 10.0
    now_wall = music.time.time()
    now_mono = music.time.monotonic()

    stale = music.GuildMusicState(guild_id=101, status="idle", updated_at=now_wall - 100.0)
    active = music.GuildMusicState(
        guild_id=102,
        status="playing",
        current=music.AgentTrack(title="active", query="active"),
        updated_at=now_wall - 100.0,
    )
    agent.states = {101: stale, 102: active}
    agent.metadata_cache_ttl = 10.0
    agent.stream_cache_ttl = 10.0
    agent._metadata_cache = {"old": (now_mono - 100.0, {"title": "old"}), "new": (now_mono, {"title": "new"})}
    agent._resolve_cache = {"old": (now_mono - 100.0, {"stream_url": "old"}), "new": (now_mono, {"stream_url": "new"})}

    agent._maybe_run_maintenance()

    assert 101 not in agent.states
    assert agent.states.get(102) is active
    assert set(agent._metadata_cache) == {"new"}
    assert set(agent._resolve_cache) == {"new"}


def test_maintenance_preserves_idle_state_with_background_work(music):
    async def scenario():
        agent = music.MusicAgent()
        agent.maintenance_interval_seconds = 0.0
        agent.state_idle_ttl_seconds = 10.0
        gid = 103
        state = music.GuildMusicState(guild_id=gid, status="idle", updated_at=music.time.time() - 100.0)
        agent.states[gid] = state
        task = asyncio.create_task(asyncio.sleep(999))
        agent._prefetch_tasks[f"{gid}:candidate"] = task
        try:
            agent._maybe_run_maintenance()
            assert agent.states.get(gid) is state
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run(scenario())
