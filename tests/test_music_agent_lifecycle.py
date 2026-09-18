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
