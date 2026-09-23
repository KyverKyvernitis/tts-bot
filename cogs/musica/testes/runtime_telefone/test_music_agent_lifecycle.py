import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
MUSIC = ROOT / "cogs/musica/runtime_telefone/agente/servidor.py"


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

    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setitem(sys.modules, "discord", discord)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)
    # O runtime musical é modular; remova os módulos carregados com o stub de
    # Discord do teste anterior antes de importar o entrypoint novamente.
    for loaded_name in list(sys.modules):
        if loaded_name.startswith("cogs.musica.runtime_telefone.agente"):
            sys.modules.pop(loaded_name, None)
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
    path = ROOT / "cogs/musica/runtime_telefone/agente/ciclo_vida.py"
    spec = importlib.util.spec_from_file_location("musica_runtime_ciclo_vida_unit", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    first, replacement = object(), object(); registry={"key": replacement}
    assert mod.remove_owned_task(registry, "key", first) is False
    assert registry["key"] is replacement
    assert mod.remove_owned_task(registry, "key", replacement) is True
    assert "key" not in registry


def test_phone_worker_music_dependencies_do_not_require_wavelink():
    source = (ROOT / "cogs/musica/runtime_telefone/ponte_worker/telemetria.py").read_text(encoding="utf-8")
    block = source.split("def music_voice_dependency_specs()", 1)[1].split("\n\ndef ", 1)[0]
    assert '"wavelink"' not in block
    assert '"yt-dlp"' in block


def test_phone_worker_music_health_treats_tts_providers_as_optional():
    source = (ROOT / "cogs/musica/runtime_telefone/ponte_worker/telemetria.py").read_text(encoding="utf-8")
    block = source.split("def music_voice_dependency_specs()", 1)[1].split("\n\ndef ", 1)[0]
    assert '"gTTS": {"module": "gtts", "pip": "gTTS==2.5.4", "optional": True}' in block
    assert '"edge-tts": {"module": "edge_tts", "pip": "edge-tts==7.2.8", "optional": True}' in block


def test_music_agent_safe_installer_uses_lightweight_ytdlp_package():
    source = (ROOT / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh").read_text(encoding="utf-8")
    assert 'safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp" light' in source
    assert 'yt-dlp[default]' not in source


def test_music_agent_autostart_is_not_turbo_only():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    integration = (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").read_text(encoding="utf-8")
    assert "ensure_music_agent_for_turbo_if_needed" not in source + integration
    assert "musica_ensure_agent_if_needed()" in integration
    companions = source.split("ensure_runtime_companions()", 1)[1].split("\n}\n", 1)[0]
    assert "musica_ensure_agent_if_needed" in companions
    assert "is_turbo_profile || return 0" not in integration.split("musica_ensure_agent_if_needed()", 1)[1].split("\n}\n", 1)[0]


def test_phone_worker_healthy_paths_supervise_music_agent_before_success():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    already_online = source.split('log "worker Termux já saudável; pid=$existing_pid"', 1)[0].rsplit("else", 1)[1]
    assert "ensure_runtime_companions" in already_online
    assert already_online.index("ensure_runtime_companions") < already_online.index("run_post_start_maintenance_async")

    just_started = source.split('if worker_healthy_for_pid "$child_pid"; then', 1)[1].split("\nfi", 1)[0]
    assert "ensure_runtime_companions" in just_started
    assert just_started.index("ensure_runtime_companions") < just_started.index("run_post_start_maintenance_async")


def test_phone_worker_maintenance_lock_recovers_orphaned_directory():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    acquire = source.split("acquire_maintenance_lock()", 1)[1].split("\n}\n", 1)[0]
    release = source.split("release_maintenance_lock()", 1)[1].split("\n}\n", 1)[0]
    assert 'owner_file="$MAINT_LOCK_DIR/owner.pid"' in acquire
    assert "printf '%s\\n' \"$BASHPID\"" in acquire
    assert 'maintenance_lock_owner_alive "$old_pid"' in acquire
    assert 'rm -rf "$MAINT_LOCK_DIR"' in acquire
    assert '"$owner" == "$BASHPID"' in release

def test_music_agent_supervisor_runs_agent_from_active_release():
    source = (ROOT / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh").read_text(encoding="utf-8")
    assert 'RUNTIME_DIR="${PHONE_WORKER_RELEASE_DIR:-$WORKER_DIR}"' in source
    assert 'AGENT_FILE="$RUNTIME_DIR/$AGENT_RELATIVE"' in source
    assert 'cd "$RUNTIME_DIR" || exit 1' in source
    assert 'exec "$PYTHON_BIN" -m "$AGENT_MODULE"' in source


def test_phone_worker_autostart_prefers_active_release_music_supervisor():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    integration = (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").read_text(encoding="utf-8")
    assert "load_music_runtime_hooks()" in source
    assert "musica_active_agent_start_command()" in integration
    assert '$release/cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh' in integration
    assert 'PHONE_WORKER_RELEASE_DIR="$release"' in integration
    assert 'MUSIC_AGENT_ENV="$MUSIC_AGENT_ENV_FILE"' in integration


def test_phone_worker_music_autostart_keeps_supervisor_output_for_audit():
    integration = (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").read_text(encoding="utf-8")
    block = integration.split("musica_ensure_agent_if_needed()", 1)[1].split("\n}\n", 1)[0]
    assert '"$start_command" || rc=$?' in block
    assert '"$start_command" >/dev/null 2>&1' not in block
    assert 'rc=$rc' in block
    assert "return 0" in block


def test_phone_worker_music_dependency_bootstrap_is_profile_independent_and_lightweight():
    source = (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").read_text(encoding="utf-8")
    ytdlp = source.split("musica_ensure_ytdlp_deps_if_needed()", 1)[1].split("\n}\n", 1)[0]
    music_deps = source.split("musica_ensure_agent_deps_if_needed()", 1)[1].split("\n}\n", 1)[0]
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
    source = (ROOT / "cogs/musica/runtime_telefone/agente/tts.py").read_text(encoding="utf-8")
    block = source.split("async def _prepare_tts_source", 1)[1].split("\n    async def cmd_tts", 1)[0]
    assert "provider_module = {'gtts': 'gtts', 'edge': 'edge_tts'}.get(engine)" in block
    assert "provider TTS {engine} indisponível no Music Agent; envie áudio pré-sintetizado" in block
    assert block.index("importlib.import_module(provider_module)") < block.index("discord.FFmpegPCMAudio(reader, pipe=True")


def test_phone_worker_music_status_forwards_compact_guild_query():
    source = (ROOT / "cogs/musica/runtime_telefone/ponte_worker/proxy.py").read_text(encoding="utf-8")
    block = source.split("def proxy_music_agent", 1)[1]
    assert 'guild_id = int(body.get("guild_id") or 0)' in block
    assert '"compact": "1" if compact else "0"' in block
    assert 'known_revision = str(body.get("known_revision") or "").strip()' in block
    assert 'params["known_revision"] = known_revision' in block
    assert 'urllib.parse.urlencode' in block
    worker_source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    assert '_phone_worker_music_bridge_module("proxy").proxy_music_agent' in worker_source



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


def test_mixer_linear_volume_keeps_fast_path_without_soft_limiter(music):
    class Source:
        def __init__(self):
            self.frames = [b"\x01\x00" * 1920, b""]
        def read(self):
            return self.frames.pop(0)
        def cleanup(self):
            return None

    class FastAudio:
        def __init__(self):
            self.mul_calls = []
        def mul(self, frame, width, volume):
            self.mul_calls.append((len(frame), width, volume))
            return b"z" * len(frame)

    async def scenario():
        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Source(),
            music_volume=0.8,
        )
        fast = FastAudio()
        mixer._audioop_module = fast
        mixer._scale_boosted_frame = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("limiter não pode entrar no caminho <=100%")
        )
        assert mixer.read() == b"z" * 3840
        assert fast.mul_calls == [(3840, 2, 0.8)]

    run(scenario())


def test_mixer_boost_soft_limits_only_peak_region(music):
    from array import array

    class Source:
        def read(self):
            return b""
        def cleanup(self):
            return None

    async def scenario():
        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Source(),
            music_volume=1.5,
        )
        raw = array("h", [1000, -1000, 20000, -20000, 30000, -30000]).tobytes()
        out = array("h")
        out.frombytes(mixer._scale_frame(raw, 1.5))

        # Sinais com folga continuam lineares mesmo em boost.
        assert out[0] == 1500
        assert out[1] == -1500
        assert out[2] == 30000
        assert out[3] == -30000
        # Picos que clipariam em 45k são comprimidos, não achatados em 32767.
        assert 30000 < out[4] < 32767
        assert -32767 < out[5] < -30000
        assert out[4] == -out[5]

    run(scenario())


def test_music_agent_volume_is_hard_capped_at_public_150_percent(music):
    class Source:
        def __init__(self):
            self.values = []
        def set_music_volume(self, value):
            self.values.append(value)

    class Player:
        def __init__(self):
            self.source = Source()

    async def scenario():
        agent = music.MusicAgent()
        gid = 991
        st = music.GuildMusicState(guild_id=gid)
        st.player = Player()
        agent.states[gid] = st

        result = await agent.cmd_volume({"guild_id": gid, "volume": 999})

        assert result["volume"] == 150
        assert result["normal_volume"] == 150
        assert st.volume_percent == 150
        assert st.normal_volume_percent == 150
        assert st.player.source.values == [1.5]

    run(scenario())


def test_mixer_constructor_caps_direct_boost_at_150_percent(music):
    class Source:
        def read(self):
            return b""
        def cleanup(self):
            return None

    async def scenario():
        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=Source(),
            music_volume=9.0,
        )
        assert mixer.normal_music_volume == 1.5
        mixer.set_music_volume(8.0)
        assert mixer.normal_music_volume == 1.5

    run(scenario())


def test_selection_prefetch_idle_usa_prioridade_alta_e_apenas_top1(music):
    async def scenario():
        agent = music.MusicAgent()
        captured = []

        async def fake_prefetch(body, track_meta, query, cache_key):
            captured.append((dict(body), dict(track_meta), query, cache_key))

        agent._prefetch_track = fake_prefetch
        result = await agent.cmd_prefetch({
            "guild_id": 501,
            "prefetch_kind": "selection",
            "limit": 3,
            "tracks": [
                {"title": "A", "webpage_url": "https://youtube.test/a"},
                {"title": "B", "webpage_url": "https://youtube.test/b"},
                {"title": "C", "webpage_url": "https://youtube.test/c"},
            ],
        })
        await asyncio.sleep(0)
        assert result["accepted"] == 1
        assert len(captured) == 1
        body, track_meta, _, _ = captured[0]
        assert track_meta["title"] == "A"
        assert body["_prefetch_priority"] == agent.selection_prefetch_idle_priority
        assert body["prefetch_kind"] == "selection"

    run(scenario())


def test_selection_prefetch_com_musica_ativa_fica_em_background(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 502
        agent.states[gid] = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="Atual", query="atual"),
            status="playing",
        )
        captured = []

        async def fake_prefetch(body, track_meta, query, cache_key):
            captured.append(dict(body))

        agent._prefetch_track = fake_prefetch
        result = await agent.cmd_prefetch({
            "guild_id": gid,
            "prefetch_kind": "selection",
            "limit": 3,
            "tracks": [{"title": "A", "webpage_url": "https://youtube.test/a"}],
        })
        await asyncio.sleep(0)
        assert result["accepted"] == 1
        assert captured[0]["_prefetch_priority"] == agent.selection_prefetch_active_priority
        assert agent.selection_prefetch_active_priority > agent.selection_prefetch_idle_priority

    run(scenario())


def test_resolve_guild_waits_for_gateway_cache_instead_of_failing_first_play(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        guild_id = 927002914449424404
        channel_id = 1483224890398998659
        channel = types.SimpleNamespace(id=channel_id)

        class Guild:
            voice_client = None

            def get_channel(self, value):
                return channel if int(value) == channel_id else None

        guild = Guild()

        class Client:
            def __init__(self):
                self.calls = 0

            def is_ready(self):
                return True

            def get_guild(self, value):
                assert int(value) == guild_id
                self.calls += 1
                return guild if self.calls >= 3 else None

            def get_channel(self, _value):
                return None

        client = Client()
        agent.client = client

        async def fast_sleep(_seconds):
            return None

        monkeypatch.setenv("MUSIC_AGENT_GUILD_CACHE_WAIT_SECONDS", "1.0")
        monkeypatch.setenv("MUSIC_AGENT_GUILD_CACHE_POLL_SECONDS", "0.02")
        monkeypatch.setattr(music.asyncio, "sleep", fast_sleep)

        resolved_guild, resolved_channel = await agent._resolve_guild_and_channel(guild_id, channel_id)
        assert resolved_guild is guild
        assert resolved_channel is channel
        assert client.calls == 3

    run(scenario())


def test_command_id_dedup_ttl_cobre_janela_maxima_de_reenvio(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_COMMAND_DEDUP_TTL_SECONDS", "10")
    agent = music.MusicAgent()
    assert agent.command_dedup_ttl_seconds >= 360.0


def test_command_id_deduplica_retry_de_playback(music):
    async def scenario():
        agent = music.MusicAgent()
        chamadas = []

        async def pause(body):
            chamadas.append(dict(body))
            return {"ok": True, "state": {"status": "paused"}}

        agent.cmd_pause = pause
        body = {"action": "pause", "guild_id": 321, "command_id": "retry-same-command"}
        first = await agent.dispatch(dict(body))
        second = await agent.dispatch(dict(body))

        assert len(chamadas) == 1
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["deduplicated"] is True

    run(scenario())


def test_command_id_concorrente_espera_primeira_execucao(music):
    async def scenario():
        agent = music.MusicAgent()
        entrou = asyncio.Event()
        liberar = asyncio.Event()
        chamadas = 0

        async def pause(body):
            nonlocal chamadas
            chamadas += 1
            entrou.set()
            await liberar.wait()
            return {"ok": True, "state": {"status": "paused"}}

        agent.cmd_pause = pause
        body = {"action": "pause", "guild_id": 654, "command_id": "retry-concurrent"}
        first = asyncio.create_task(agent.dispatch(dict(body)))
        await entrou.wait()
        second = asyncio.create_task(agent.dispatch(dict(body)))
        await asyncio.sleep(0)
        assert chamadas == 1
        liberar.set()
        one, two = await asyncio.gather(first, second)
        assert chamadas == 1
        assert one["ok"] is True
        assert two["deduplicated"] is True

    run(scenario())


def _virtual_cursor(offset=25, *, exhausted=False):
    return {
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/test",
        "title": "Playlist Grande",
        "resource_type": "playlist",
        "resource_id": "test",
        "next_offset": offset,
        "total_tracks": None,
        "exhausted": exhausted,
    }


def _virtual_marker(music, agent, *, offset=25):
    cursor = _virtual_cursor(offset)
    return agent._agent_track_from_metadata(
        {
            "title": "Playlist Grande",
            "webpage_url": cursor["source_url"],
            "source": "playlist-virtual",
            "virtual_playlist_cursor": cursor,
            "requester_id": 7,
            "requester_name": "Core",
        },
        body={"requester_id": 7, "requester_name": "Core"},
        fallback_query="",
    )


def test_virtual_playlist_marker_is_not_resolved_as_audio(music):
    agent = music.MusicAgent()
    marker = _virtual_marker(music, agent)

    assert marker.is_virtual_playlist_marker is True
    assert marker.query == ""
    assert marker.transport_hint == "playlist-cursor"
    assert marker.virtual_playlist_cursor["next_offset"] == 25

    gid = 801
    st = music.GuildMusicState(guild_id=gid, queue=[marker], status="playing")
    agent.states[gid] = st
    agent.prefetch_enabled = True
    agent._query_from_track_meta = lambda *a, **k: (_ for _ in ()).throw(AssertionError("marker não pode ser resolvido"))
    agent._schedule_next_queue_prefetch(gid)
    assert agent._prefetch_tasks == {}


def test_virtual_playlist_public_state_hides_marker_and_preserves_logical_order(music):
    agent = music.MusicAgent()
    marker = _virtual_marker(music, agent)
    st = music.GuildMusicState(
        guild_id=802,
        queue=[
            music.AgentTrack(title="Antes", query="antes"),
            marker,
            music.AgentTrack(title="Manual depois", query="manual"),
        ],
    )

    public = st.public()
    assert public["queue_size"] == 2
    assert [item["title"] for item in public["queue"]] == ["Antes"]
    assert public["virtual_playlist"]["materialized_before"] == 1
    assert public["virtual_playlist"]["cursor"]["next_offset"] == 25


def test_playlist_refill_replaces_marker_atomically_and_keeps_later_queue(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 803
        marker = _virtual_marker(music, agent, offset=25)
        st = music.GuildMusicState(
            guild_id=gid,
            status="playing",
            current=music.AgentTrack(title="Atual", query="atual"),
            queue=[
                music.AgentTrack(title="Antes", query="antes"),
                marker,
                music.AgentTrack(title="Manual depois", query="manual"),
            ],
        )
        agent.states[gid] = st
        agent._schedule_next_queue_prefetch = lambda *a, **k: None

        result = await agent.cmd_playlist_refill(
            {
                "guild_id": gid,
                "expected_cursor": _virtual_cursor(25),
                "next_cursor": _virtual_cursor(27),
                "tracks": [
                    {"title": "Faixa 26", "query": "ytsearch1:faixa 26"},
                    {"title": "Faixa 27", "query": "ytsearch1:faixa 27"},
                ],
                "requester_id": 7,
                "requester_name": "Core",
            }
        )

        assert result["ok"] is True and result["ignored"] is False and result["added"] == 2
        assert [item.title for item in st.queue] == [
            "Antes",
            "Faixa 26",
            "Faixa 27",
            "Playlist Grande",
            "Manual depois",
        ]
        assert st.queue[3].is_virtual_playlist_marker is True
        assert st.queue[3].virtual_playlist_cursor["next_offset"] == 27

    run(scenario())


def test_playlist_refill_ignores_stale_cursor_without_duplicate_insertion(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 804
        marker = _virtual_marker(music, agent, offset=50)
        st = music.GuildMusicState(guild_id=gid, status="playing", queue=[marker])
        agent.states[gid] = st

        result = await agent.cmd_playlist_refill(
            {
                "guild_id": gid,
                "expected_cursor": _virtual_cursor(25),
                "next_cursor": _virtual_cursor(50),
                "tracks": [{"title": "Duplicada", "query": "ytsearch1:duplicada"}],
            }
        )

        assert result["ok"] is True and result["ignored"] is True and result["added"] == 0
        assert st.queue == [marker]

    run(scenario())


def test_play_next_waits_on_virtual_cursor_without_ending_session(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 805
        marker = _virtual_marker(music, agent)
        current = music.AgentTrack(title="Última materializada", query="ultima")
        st = music.GuildMusicState(guild_id=gid, current=current, queue=[marker], status="playing")
        agent.states[gid] = st
        idle_calls = []
        agent._schedule_idle_disconnect = lambda guild_id: idle_calls.append(guild_id)

        await agent._play_next(gid)

        assert st.current is None
        assert st.queue == [marker]
        assert st.status == "queued"
        assert st.last_event == "playlist_refill_needed"
        assert [item.title for item in st.history] == ["Última materializada"]
        assert idle_calls == []

    run(scenario())


def test_shuffle_virtual_com_um_item_pronto_nao_atravessa_marker(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 806
        marker = _virtual_marker(music, agent)
        before = music.AgentTrack(title="Antes", query="antes")
        after = music.AgentTrack(title="Depois", query="depois")
        st = music.GuildMusicState(guild_id=gid, queue=[before, marker, after])
        agent.states[gid] = st

        result = await agent.cmd_shuffle({"guild_id": gid})

        assert result["ok"] is True
        assert result["shuffled"] is True
        assert st.queue == [before, marker, after]
        assert st.virtual_shuffle_active is True

    run(scenario())


def test_failed_playlist_track_is_skipped_without_entering_history(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 1601
        st = music.GuildMusicState(guild_id=gid, voice_channel_id=0, text_channel_id=20)
        bad = music.AgentTrack(title="quebrada", query="ytsearch1:quebrada")
        good = music.AgentTrack(title="boa", query="ytsearch1:boa")
        st.queue = [bad, good]
        agent.states[gid] = st
        events = []

        async def resolve(query, **kwargs):
            if "quebrada" in query:
                raise RuntimeError("não encontrei fonte tocável")
            return music.AgentTrack(title="boa", query=query, stream_url="https://audio.example/boa.opus")

        async def play_direct(guild_id, track, **kwargs):
            events.append(("played", track.title))

        agent.resolve_track = resolve
        agent._play_direct_voice = play_direct
        agent._should_use_direct_voice = lambda track: True
        agent.log = lambda event, **fields: events.append((event, fields.get("title", "")))

        await agent._play_next(gid)

        assert st.current is not None
        assert st.current.title == "boa"
        assert st.history == []
        assert not st.queue
        assert ("track_failed_skipped", "quebrada") in events
        assert ("played", "boa") in events

    run(scenario())


def test_playlist_refill_defensively_caps_materialized_batch(music, monkeypatch):
    async def scenario():
        monkeypatch.setenv("MUSIC_AGENT_PLAYLIST_REFILL_MAX_ITEMS", "2")
        agent = music.MusicAgent()
        gid = 1602
        cursor = {
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/abc1234567890123",
            "title": "Grande",
            "resource_type": "playlist",
            "resource_id": "abc1234567890123",
            "next_offset": 25,
            "total_tracks": None,
            "exhausted": False,
        }
        marker = music.AgentTrack(
            title="Grande",
            source="playlist-virtual",
            transport_hint="playlist-cursor",
            virtual_playlist_cursor=dict(cursor),
        )
        st = music.GuildMusicState(guild_id=gid)
        st.current = music.AgentTrack(title="tocando", stream_url="https://audio.example/current")
        st.status = "playing"
        st.queue = [marker]
        agent.states[gid] = st
        agent.log = lambda *args, **kwargs: None

        tracks = [
            {"title": f"faixa {index}", "query": f"ytsearch1:faixa {index}"}
            for index in range(5)
        ]
        next_cursor = dict(cursor)
        next_cursor["next_offset"] = 30

        result = await agent.cmd_playlist_refill({
            "guild_id": gid,
            "tracks": tracks,
            "expected_cursor": cursor,
            "next_cursor": next_cursor,
        })

        assert result["added"] == 2
        assert [item.title for item in st.queue[:2]] == ["faixa 0", "faixa 1"]
        assert st.queue[2].is_virtual_playlist_marker

    run(scenario())


def test_failed_startup_track_advances_to_virtual_playlist_marker(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 1603
        cursor = {
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/abc1234567890123",
            "next_offset": 1,
            "exhausted": False,
        }
        bad = music.AgentTrack(title="quebrada", query="ytsearch1:quebrada")
        marker = music.AgentTrack(
            title="Playlist",
            source="playlist-virtual",
            transport_hint="playlist-cursor",
            virtual_playlist_cursor=cursor,
        )
        st = music.GuildMusicState(guild_id=gid, voice_channel_id=0)
        st.queue = [bad, marker]
        agent.states[gid] = st
        events = []

        async def resolve(*args, **kwargs):
            raise RuntimeError("indisponível")

        agent.resolve_track = resolve
        agent.log = lambda event, **fields: events.append(event)

        await agent._play_next(gid)

        assert st.current is None
        assert st.status == "queued"
        assert st.last_event == "playlist_refill_needed"
        assert st.queue == [marker]
        assert "track_failed_skipped" in events
        assert "playlist_refill_needed" in events

    run(scenario())


def test_playlist_track_key_prefers_query_over_shared_collection_url(music):
    agent = music.MusicAgent()
    playlist = "https://open.spotify.com/playlist/shared"
    juliet = music.AgentTrack(
        title="Cavetown - Juliet",
        query="ytsearch1:Cavetown - Juliet official audio",
        webpage_url="",
        original_url=playlist,
    )
    home = music.AgentTrack(
        title="Cavetown - Home",
        query="ytsearch1:Cavetown - Home official audio",
        webpage_url="",
        original_url=playlist,
    )
    assert agent._track_key(juliet) != agent._track_key(home)


def test_skip_rejects_prefetched_stream_reused_from_previous_playlist_item(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 2201
        playlist = "https://open.spotify.com/playlist/shared"
        previous = music.AgentTrack(
            title="Cavetown - Juliet",
            query="ytsearch1:Cavetown - Juliet official audio",
            webpage_url="https://www.youtube.com/watch?v=juliet",
            original_url=playlist,
            stream_url="https://rr.example/SAME",
        )
        nxt = music.AgentTrack(
            title="Cavetown - Home",
            query="ytsearch1:Cavetown - Home official audio",
            webpage_url="https://www.youtube.com/watch?v=home",
            original_url=playlist,
            stream_url="https://rr.example/SAME",
            transport_hint="direct-cache",
        )
        st = music.GuildMusicState(guild_id=gid, current=previous, queue=[nxt], status="playing")
        agent.states[gid] = st
        agent._cancel_prefetch_tasks = lambda *args, **kwargs: 0
        agent._cancel_active_resolve = lambda *args, **kwargs: False
        seen = []

        async def stop_player(_player, *, disconnect=False):
            return None

        async def play_next(guild_id, *, preserve_current_to_history=True):
            item = agent.states[guild_id].queue.pop(0)
            seen.append(item)
            agent.states[guild_id].current = item

        agent._stop_player_instance = stop_player
        agent._play_next = play_next
        await agent.cmd_skip({"guild_id": gid})

        assert len(seen) == 1
        assert seen[0].title == "Cavetown - Home"
        assert seen[0].stream_url == ""
        assert seen[0].transport_hint == "metadata-lazy"

    run(scenario())


def test_skip_preserva_prefetch_da_proxima_faixa_em_andamento(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 601
        next_track = music.AgentTrack(title="next", query="next")
        state = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="current", query="current"),
            queue=[next_track],
            status="playing",
        )
        agent.states[gid] = state
        query = agent._query_from_track_meta(next_track.public(), fallback_query=next_track.query)
        key = agent._guild_prefetch_key(gid, agent._resolve_cache_key(query, next_track.public()))
        prefetch = asyncio.create_task(asyncio.sleep(60))
        discarded = asyncio.create_task(asyncio.sleep(60))
        agent._prefetch_tasks = {key: prefetch, f"{gid}:old": discarded}
        agent._prefetch_resolving.add(key)
        agent._stop_player_instance = lambda *args, **kwargs: asyncio.sleep(0)
        async def next_play(_guild_id):
            assert not prefetch.cancelled()
            assert discarded.cancelled()
        agent._play_next = next_play
        try:
            result = await agent.cmd_skip({"guild_id": gid})
            assert result["ok"] is True
            assert agent._prefetch_tasks.get(key) is prefetch
            assert not prefetch.cancelled()
        finally:
            prefetch.cancel()
            await asyncio.gather(prefetch, discarded, return_exceptions=True)

    run(scenario())


def test_eof_no_meio_de_faixa_recupera_sem_erro_ffmpeg(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 602
        state = music.GuildMusicState(
            guild_id=gid,
            current=music.AgentTrack(title="long song", query="long song", duration=180.0),
            playback_token=10,
            started_monotonic=music.time.monotonic() - 45.0,
            status="playing",
        )
        agent.states[gid] = state
        recovered = []
        async def recover(_guild_id, *, played_for, reason):
            recovered.append((played_for, reason))
            return True
        agent._recover_current_stream = recover
        await agent._direct_after(gid, None, 10)
        assert len(recovered) == 1
        assert recovered[0][0] >= 45.0
        assert recovered[0][1] == "direct_after_early_end"
        assert state.current.title == "long song"

    run(scenario())


def test_mixer_persistente_carrega_tts_na_troca_de_faixa(music, monkeypatch):
    class PCM:
        def __init__(self, url, **kwargs):
            self.frames = [b"\x04\x00" * 1920, b""]
            self.cleaned = False
        def read(self):
            return self.frames.pop(0) if self.frames else b""
        def cleanup(self):
            self.cleaned = True

    class Speech:
        def __init__(self):
            self.frames = [b"\x02\x00" * 1920] * 4 + [b""]
            self.cleaned = False
        def read(self):
            return self.frames.pop(0)
        def cleanup(self):
            self.cleaned = True

    class Voice:
        def __init__(self, channel_id):
            self.channel = types.SimpleNamespace(id=channel_id, bitrate=128_000)
            self.source = None
            self.calls = 0
            self.playing = False
        def is_connected(self): return True
        def is_playing(self): return self.playing
        def is_paused(self): return False
        def play(self, source, after=None, **kwargs):
            self.calls += 1
            self.source = source
            self.after = after
            self.playing = True
        def stop(self): self.playing = False

    async def scenario():
        monkeypatch.setattr(music.discord, "FFmpegPCMAudio", PCM)
        agent = music.MusicAgent()
        agent._loop = asyncio.get_running_loop()
        gid, channel_id = 603, 904
        voice = Voice(channel_id)
        agent._ensure_direct_voice_client = lambda _guild_id: asyncio.sleep(0, result=(voice, False))
        agent._schedule_next_queue_prefetch = lambda *args, **kwargs: None
        agent._schedule_idle_disconnect = lambda *args, **kwargs: None
        async def confirm(_voice, source, *, max_delay):
            assert source.read()
            return 0.0
        agent._confirm_direct_playback = confirm
        first = music.AgentTrack(title="one", stream_url="https://cdn/one", duration=0.1)
        second = music.AgentTrack(title="two", stream_url="https://cdn/two", duration=0.1)
        state = music.GuildMusicState(guild_id=gid, voice_channel_id=channel_id, current=first, queue=[second])
        agent.states[gid] = state

        await agent._play_direct_voice(gid, first)
        mixer = voice.source
        speech = Speech()
        done = mixer.add_tts(speech)
        assert mixer.read()  # EOF da primeira música; TTS continua
        for _ in range(20):
            if state.current is second and state.status == "playing":
                break
            await asyncio.sleep(0.01)
        assert state.current is second and state.status == "playing"
        assert voice.calls == 1
        assert voice.source is mixer
        assert mixer.has_tts() and not done.done()
        while mixer.has_tts():
            mixer.read()
            await asyncio.sleep(0)
        assert done.done() and speech.cleaned
        third = music.AgentTrack(title="three", stream_url="https://cdn/three", duration=0.1)
        state.queue.append(third)
        second_speech = Speech()
        second_done = mixer.add_tts(second_speech)
        await agent.cmd_skip({"guild_id": gid})
        assert voice.calls == 1
        assert voice.source is mixer
        assert state.current is third
        assert mixer.has_tts() and not second_done.done()
        recoveries = []
        async def recover(_guild_id, *, played_for, reason):
            recoveries.append((state.current.title, reason))
            return True
        agent._recover_current_stream = recover
        voice.after(RuntimeError("transport dropped"))
        for _ in range(20):
            if recoveries:
                break
            await asyncio.sleep(0.01)
        assert recoveries == [("three", "direct_after_error")]
        while mixer.has_tts():
            mixer.read()
            await asyncio.sleep(0)
        assert second_done.done() and second_speech.cleaned
        mixer.cleanup()

    run(scenario())


def test_queue_remote_mutations_preserve_virtual_marker(music):
    async def scenario():
        agent = music.MusicAgent(); gid = 901
        marker = music.AgentTrack(
            title="Playlist",
            source="playlist-virtual",
            transport_hint="playlist-cursor",
            virtual_playlist_cursor={"provider": "spotify_public", "source_url": "https://open.spotify.com/playlist/x", "next_offset": 25},
        )
        manual = music.AgentTrack(title="manual", query="manual")
        st = music.GuildMusicState(guild_id=gid)
        st.queue = [
            music.AgentTrack(title="a", query="a"),
            music.AgentTrack(title="b", query="b"),
            music.AgentTrack(title="c", query="c"),
            marker,
            manual,
        ]
        agent.states[gid] = st
        agent._schedule_next_queue_prefetch = lambda *a, **k: None

        moved = await agent.cmd_queue_move({"guild_id": gid, "from_position": 3, "to_position": 1})
        assert moved["ok"] is True
        assert [item.title for item in st.queue] == ["c", "a", "b", "Playlist", "manual"]

        removed = await agent.cmd_queue_remove({"guild_id": gid, "position": 2})
        assert removed["ok"] is True
        assert removed["removed"]["title"] == "a"
        assert [item.title for item in st.queue] == ["c", "b", "Playlist", "manual"]
    run(scenario())


def test_queue_play_now_is_authoritative_and_does_not_drop_cursor(music):
    async def scenario():
        agent = music.MusicAgent(); gid = 902
        marker = music.AgentTrack(
            title="Playlist",
            source="playlist-virtual",
            transport_hint="playlist-cursor",
            virtual_playlist_cursor={"provider": "spotify_public", "source_url": "https://open.spotify.com/playlist/x", "next_offset": 25},
        )
        st = music.GuildMusicState(guild_id=gid)
        st.current = music.AgentTrack(title="current", query="current")
        st.status = "playing"
        st.queue = [music.AgentTrack(title="a", query="a"), music.AgentTrack(title="b", query="b"), marker]
        agent.states[gid] = st
        agent._guard_distinct_next_stream = lambda *a, **k: False
        agent._next_resolving_prefetch_keys = lambda *a, **k: set()
        async def fake_next(_gid, preserve_current_to_history=True):
            st.current = st.queue.pop(0)
            st.status = "playing"
        agent._play_next = fake_next

        result = await agent.cmd_queue_play_now({"guild_id": gid, "position": 2})
        assert result["ok"] is True
        assert st.current.title == "b"
        assert [item.title for item in st.queue] == ["a", "Playlist"]
        assert st.queue[-1].is_virtual_playlist_marker
        assert [item.title for item in st.history][-1:] == ["current"]
    run(scenario())


def test_queue_clear_remote_keeps_current_but_removes_virtual_tail(music):
    async def scenario():
        agent = music.MusicAgent(); gid = 903
        marker = music.AgentTrack(title="Playlist", transport_hint="playlist-cursor", virtual_playlist_cursor={"next_offset": 25})
        st = music.GuildMusicState(guild_id=gid, current=music.AgentTrack(title="current", query="current"))
        st.queue = [music.AgentTrack(title="next", query="next"), marker]
        st.virtual_shuffle_active = True
        agent.states[gid] = st
        result = await agent.cmd_queue_clear({"guild_id": gid})
        assert result["ok"] is True
        assert result["removed_count"] == 1
        assert st.current.title == "current"
        assert st.queue == []
        assert st.virtual_shuffle_active is False
    run(scenario())


def test_virtual_playlist_shuffle_works_without_materializing_collection(music):
    async def scenario():
        agent = music.MusicAgent(); gid = 904
        marker = music.AgentTrack(
            title="Playlist",
            transport_hint="playlist-cursor",
            virtual_playlist_cursor={"provider": "spotify_public", "source_url": "https://open.spotify.com/playlist/x", "next_offset": 25},
        )
        st = music.GuildMusicState(guild_id=gid)
        st.queue = [music.AgentTrack(title=str(i), query=str(i)) for i in range(8)] + [marker]
        agent.states[gid] = st
        agent._schedule_next_queue_prefetch = lambda *a, **k: None
        result = await agent.cmd_shuffle({"guild_id": gid})
        assert result["ok"] is True and result["shuffled"] is True and result.get("virtual") is True
        assert st.virtual_shuffle_active is True
        assert st.queue[-1] is marker
        assert sorted(item.title for item in st.queue[:-1]) == [str(i) for i in range(8)]
    run(scenario())


def test_remote_public_queue_preview_covers_virtual_window(music):
    st = music.GuildMusicState(guild_id=905)
    st.queue = [music.AgentTrack(title=str(i), query=str(i)) for i in range(25)]
    payload = st.public()
    assert len(payload["queue"]) == 25
    assert payload["queue_size"] == 25


def test_voice_connect_singleflight_serializes_concurrent_callers(music):
    class VoiceClient:
        def __init__(self, channel):
            self.channel = channel
        def is_connected(self):
            return True

    class Guild:
        voice_client = None
        def __init__(self, channel):
            self.channel = channel
        def get_channel(self, channel_id):
            return self.channel if channel_id == self.channel.id else None

    class Channel:
        id = 1901
        def __init__(self):
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.guild = None
        async def connect(self, self_deaf=True):
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            voice = VoiceClient(self)
            self.guild.voice_client = voice
            return voice

    async def scenario():
        agent = music.MusicAgent()
        gid = 1900
        channel = Channel()
        guild = Guild(channel)
        channel.guild = guild
        agent.states[gid] = music.GuildMusicState(guild_id=gid, voice_channel_id=channel.id)
        agent.client.get_guild = lambda value: guild if value == gid else None
        agent.client.get_channel = lambda value: channel if value == channel.id else None
        agent.log = lambda *args, **kwargs: None

        first = asyncio.create_task(agent._ensure_direct_voice_client(gid))
        await asyncio.wait_for(channel.entered.wait(), timeout=1.0)
        second = asyncio.create_task(agent._ensure_direct_voice_client(gid))
        await asyncio.sleep(0)
        assert channel.calls == 1
        channel.release.set()
        result1, result2 = await asyncio.gather(first, second)

        assert channel.calls == 1
        assert result1[0] is result2[0]
        assert sorted([result1[1], result2[1]]) == [False, True]
        assert agent._voice_connect_locks == {}
        assert agent._voice_connect_lock_users == {}

    run(scenario())


def test_voice_connect_recovers_discord_already_connected_race(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_VOICE_RECONNECT_GRACE_SECONDS", "0")

    class VoiceClient:
        def __init__(self, channel):
            self.channel = channel
        def is_connected(self):
            return True
        async def move_to(self, channel):
            self.channel = channel

    class Channel:
        id = 1903
        def __init__(self):
            self.guild = None
            self.calls = 0
        async def connect(self, self_deaf=True):
            self.calls += 1
            # Reproduz a janela em que discord.py já registrou o cliente, mas a
            # outra coroutine venceu a corrida e channel.connect() reclama.
            self.guild.voice_client = VoiceClient(self)
            raise RuntimeError("Already connected to a voice channel.")

    class Guild:
        voice_client = None
        def __init__(self, channel):
            self.channel = channel
        def get_channel(self, channel_id):
            return self.channel if channel_id == self.channel.id else None

    async def scenario():
        agent = music.MusicAgent()
        gid = 1902
        channel = Channel()
        guild = Guild(channel)
        channel.guild = guild
        agent.states[gid] = music.GuildMusicState(guild_id=gid, voice_channel_id=channel.id)
        agent.client.get_guild = lambda value: guild if value == gid else None
        agent.client.get_channel = lambda value: channel if value == channel.id else None
        events = []
        agent.log = lambda event, **fields: events.append(event)

        voice, created = await agent._ensure_direct_voice_client(gid)
        assert voice is guild.voice_client
        assert created is False
        assert channel.calls == 1
        assert "voice_connect_race_recovered" in events

    run(scenario())


def test_stale_disconnected_voice_client_is_cleaned_before_reconnect(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_VOICE_RECONNECT_GRACE_SECONDS", "0")

    class StaleVoice:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel
            self.disconnected = False
        def is_connected(self):
            return False
        async def disconnect(self, force=False):
            self.disconnected = True
            self.guild.voice_client = None

    class FreshVoice:
        def __init__(self, channel):
            self.channel = channel
        def is_connected(self):
            return True

    class Channel:
        id = 1905
        def __init__(self):
            self.guild = None
            self.calls = 0
        async def connect(self, self_deaf=True):
            self.calls += 1
            voice = FreshVoice(self)
            self.guild.voice_client = voice
            return voice

    class Guild:
        def __init__(self, channel):
            self.channel = channel
            self.voice_client = StaleVoice(self, channel)
        def get_channel(self, channel_id):
            return self.channel if channel_id == self.channel.id else None

    async def scenario():
        agent = music.MusicAgent()
        gid = 1904
        channel = Channel()
        guild = Guild(channel)
        stale = guild.voice_client
        channel.guild = guild
        agent.states[gid] = music.GuildMusicState(guild_id=gid, voice_channel_id=channel.id)
        agent.client.get_guild = lambda value: guild if value == gid else None
        agent.client.get_channel = lambda value: channel if value == channel.id else None
        events = []
        agent.log = lambda event, **fields: events.append(event)

        voice, created = await agent._ensure_direct_voice_client(gid)
        assert stale.disconnected is True
        assert created is True
        assert voice is guild.voice_client
        assert channel.calls == 1
        assert "voice_stale_client_cleanup" in events

    run(scenario())


def test_voice_transport_failure_retries_same_track_and_preserves_queue(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 1906
        current = music.AgentTrack(title="current", query="current", stream_url="https://media.example/current")
        next_track = music.AgentTrack(title="next", query="next", stream_url="https://media.example/next")
        st = music.GuildMusicState(guild_id=gid, queue=[current, next_track])
        agent.states[gid] = st
        events = []

        async def fail_voice(*args, **kwargs):
            raise RuntimeError("Already connected to a voice channel.")

        agent._play_direct_voice = fail_voice
        agent.log = lambda event, **fields: events.append((event, fields))

        await agent._play_next(gid)

        assert st.status == "failed"
        assert st.current is current
        assert st.queue == [next_track]
        assert st.last_error_category == "voice_transport"
        assert st.last_error_phase == "playback_start"
        assert st.play_attempt_sequence == 2
        assert current.voice_recovery_attempts == 1
        assert not any(event == "track_failed_skipped" for event, _ in events)
        assert any(event == "voice_transport_retry" for event, _ in events)
        assert any(event == "voice_transport_queue_preserved" for event, _ in events)

    run(scenario())


def test_consecutive_start_failure_circuit_preserves_remaining_queue(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_MAX_CONSECUTIVE_START_FAILURES", "2")

    async def scenario():
        agent = music.MusicAgent()
        gid = 1907
        tracks = [music.AgentTrack(title=f"bad-{n}", query=f"bad-{n}") for n in range(3)]
        st = music.GuildMusicState(guild_id=gid, queue=list(tracks))
        agent.states[gid] = st
        events = []

        async def fail_resolve(*args, **kwargs):
            raise RuntimeError("fonte indisponível")

        agent.resolve_track = fail_resolve
        agent.log = lambda event, **fields: events.append((event, fields))

        await agent._play_next(gid)

        assert st.status == "failed"
        assert st.current is tracks[1]
        assert st.queue == [tracks[2]]
        assert st.consecutive_start_failures == 2
        assert st.last_error_category == "resolution"
        assert sum(1 for event, _ in events if event == "track_failed_skipped") == 1
        assert sum(1 for event, _ in events if event == "track_failure_circuit_open") == 1

    run(scenario())


def test_direct_after_voice_loss_preserves_current_and_queue(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 2110
        current = music.AgentTrack(title="current", query="current", stream_url="https://media/current", duration=180)
        next_track = music.AgentTrack(title="next", query="next", stream_url="https://media/next")
        st = music.GuildMusicState(guild_id=gid, current=current, queue=[next_track], status="playing")
        st.started_monotonic = music.time.monotonic() - 12.0

        class Voice:
            def is_connected(self): return False

        st.player = Voice()
        agent.states[gid] = st
        agent.log = lambda *args, **kwargs: None
        agent._recover_current_stream = lambda *args, **kwargs: asyncio.sleep(0, result=False)
        scheduled = []
        agent._schedule_voice_runtime_recovery = lambda guild_id, **kwargs: scheduled.append((guild_id, kwargs)) or True

        await agent._direct_after(gid, RuntimeError("sessão de voz encerrada durante a música"), st.playback_token)

        assert st.current is current
        assert st.queue == [next_track]
        assert st.last_error_category == "voice_transport"
        assert st.last_error_phase == "runtime_playback"
        assert scheduled and scheduled[0][0] == gid
        assert scheduled[0][1]["reason"] == "direct_after_error"
        assert not any(getattr(item, "title", "") == "current" for item in st.queue)

    run(scenario())


def test_runtime_voice_recovery_retries_same_track_until_success(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_VOICE_RUNTIME_RECOVERY_ATTEMPTS", "3")
    monkeypatch.setenv("MUSIC_AGENT_VOICE_RUNTIME_RECOVERY_BASE_SECONDS", "0.2")

    async def scenario():
        agent = music.MusicAgent()
        gid = 2111
        current = music.AgentTrack(title="current", query="current", stream_url="https://media/current", duration=200)
        next_track = music.AgentTrack(title="next", query="next", stream_url="https://media/next")
        st = music.GuildMusicState(guild_id=gid, current=current, queue=[next_track], status="failed")
        agent.states[gid] = st
        events = []
        calls = 0

        async def play(_gid, track, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise music.VoiceSessionError("conexão de voz temporariamente indisponível")
            agent._set_status(st, "playing", event="test_recovered")

        agent._play_direct_voice = play
        agent.log = lambda event, **fields: events.append((event, fields))

        assert agent._schedule_voice_runtime_recovery(
            gid,
            played_for=14.0,
            reason="test",
            error="voice lost",
        ) is True
        task = agent._voice_runtime_recovery_tasks[gid]
        await task

        assert calls == 3
        assert st.current is current
        assert st.queue == [next_track]
        assert st.status == "playing"
        assert st.voice_runtime_recovery_pending is False
        assert st.voice_runtime_recovery_attempts == 3
        assert any(event == "voice_runtime_recovered" for event, _ in events)
        assert gid not in agent._voice_runtime_recovery_tasks
        assert current.start_offset_seconds > 10.0

    run(scenario())


def test_watchdog_music_health_check_is_quiet_but_keeps_transition_logs():
    integration = (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").read_text(encoding="utf-8")
    supervisor = (ROOT / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh").read_text(encoding="utf-8")
    block = integration.split("musica_ensure_agent_if_needed()", 1)[1].split("\n}\n", 1)[0]
    assert "MUSIC_AGENT_QUIET_HEALTHY=1" in block
    assert 'log "garantindo Music Agent do worker' not in block
    assert 'MUSIC_AGENT_QUIET_HEALTHY' in supervisor
    assert 'log "Music Agent iniciado com sucesso; pid=$pid"' in supervisor
    assert 'log "Music Agent online está desatualizado' in supervisor


def test_phone_worker_env_upsert_avoids_identical_rewrite():
    source = (ROOT / "deploy/termux/phone-worker/start-phone-worker.sh").read_text(encoding="utf-8")
    block = source.split("upsert_env_value()", 1)[1].split("\n}\n", 1)[0]
    assert 'grep -Fxq "${key}=${value}"' in block
    assert 'export "$key=$value"' in block
    assert "return 0" in block


def test_music_agent_start_rotates_log_and_marks_new_session():
    source = (ROOT / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh").read_text(encoding="utf-8")
    assert 'MUSIC_AGENT_LOG_MAX_BYTES' in source
    assert 'rotate_agent_log_if_needed' in source
    assert '${LOG_FILE}.1' in source
    assert '[music-agent-session] start' in source
    assert 'mark_agent_session' in source


def test_runtime_voice_loss_bypasses_stream_reresolve(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 2201
        current = music.AgentTrack(title="current", query="current", stream_url="https://media/current", duration=200)
        st = music.GuildMusicState(
            guild_id=gid,
            current=current,
            queue=[music.AgentTrack(title="next", query="next", stream_url="https://media/next")],
            status="playing",
        )
        st.playback_token = 4
        st.started_monotonic = music.time.monotonic() - 12.0
        st.player = object()  # sem is_connected -> sessão perdida
        agent.states[gid] = st
        recover_calls = []
        stream_calls = []
        events = []

        async def recover_stream(*args, **kwargs):
            stream_calls.append((args, kwargs))
            raise AssertionError("queda de voz não deve re-resolver stream antes da reconexão")

        def schedule(guild_id, **kwargs):
            recover_calls.append((guild_id, kwargs))
            return True

        agent._recover_current_stream = recover_stream
        agent._schedule_voice_runtime_recovery = schedule
        agent.log = lambda event, **fields: events.append((event, fields))

        await agent._direct_after(gid, RuntimeError("voice websocket closed"), 4)

        assert stream_calls == []
        assert len(recover_calls) == 1
        assert recover_calls[0][0] == gid
        assert recover_calls[0][1]["played_for"] > 10.0
        assert st.current is current
        assert st.queue[0].title == "next"
        preserve = [fields for event, fields in events if event == "voice_transport_playback_preserved"]
        assert preserve and preserve[-1]["stream_refresh_skipped"] is True

    run(scenario())


def test_early_end_after_voice_loss_bypasses_stream_reresolve(music):
    async def scenario():
        agent = music.MusicAgent()
        gid = 2202
        current = music.AgentTrack(title="current", query="current", stream_url="https://media/current", duration=180)
        st = music.GuildMusicState(guild_id=gid, current=current, status="playing")
        st.playback_token = 9
        st.started_monotonic = music.time.monotonic() - 8.0
        st.player = object()
        agent.states[gid] = st
        stream_calls = []
        recovery = []

        async def recover_stream(*args, **kwargs):
            stream_calls.append((args, kwargs))
            return False

        agent._recover_current_stream = recover_stream
        agent._schedule_voice_runtime_recovery = lambda guild_id, **kwargs: recovery.append((guild_id, kwargs)) or True
        agent.log = lambda *args, **kwargs: None

        await agent._direct_after(gid, None, 9)

        assert stream_calls == []
        assert recovery and recovery[0][1]["reason"] == "direct_after_early_end"
        assert st.current is current

    run(scenario())


def test_failed_voice_move_cleans_client_so_next_retry_can_reconnect(music, monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_VOICE_RECONNECT_GRACE_SECONDS", "0")

    class Channel:
        def __init__(self, channel_id):
            self.id = channel_id
            self.guild = None
            self.connect_calls = 0

        async def connect(self, self_deaf=True):
            self.connect_calls += 1
            voice = FreshVoice(self.guild, self)
            self.guild.voice_client = voice
            return voice

    class BrokenVoice:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel
            self.disconnected = False

        def is_connected(self):
            return True

        async def move_to(self, channel):
            raise RuntimeError("move failed")

        async def disconnect(self, force=False):
            self.disconnected = True
            self.guild.voice_client = None

    class FreshVoice:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel

        def is_connected(self):
            return True

    class Guild:
        id = 2203

        def __init__(self, old_channel, target):
            self.old_channel = old_channel
            self.target = target
            self.voice_client = BrokenVoice(self, old_channel)

        def get_channel(self, channel_id):
            if channel_id == self.target.id:
                return self.target
            if channel_id == self.old_channel.id:
                return self.old_channel
            return None

    async def scenario():
        agent = music.MusicAgent()
        old = Channel(3001)
        target = Channel(3002)
        guild = Guild(old, target)
        old.guild = guild
        target.guild = guild
        broken = guild.voice_client
        agent.states[guild.id] = music.GuildMusicState(guild_id=guild.id, voice_channel_id=target.id)
        agent.client.get_guild = lambda value: guild if value == guild.id else None
        agent.client.get_channel = lambda value: target if value == target.id else None
        events = []
        agent.log = lambda event, **fields: events.append(event)

        with pytest.raises(RuntimeError, match="falha ao mover sessão de voz"):
            await agent._ensure_direct_voice_client(guild.id)
        assert broken.disconnected is True
        assert guild.voice_client is None
        assert "voice_move_failed_cleanup" in events

        voice, created = await agent._ensure_direct_voice_client(guild.id)
        assert created is True
        assert voice.channel is target
        assert target.connect_calls == 1

    run(scenario())


def test_voice_registry_fallback_reuses_client_when_guild_cache_is_temporarily_empty(music):
    class Voice:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel

        def is_connected(self):
            return True

    class Channel:
        id = 3102

        def __init__(self):
            self.connect_calls = 0

        async def connect(self, self_deaf=True):
            self.connect_calls += 1
            raise AssertionError("não deve abrir segunda conexão")

    class Guild:
        id = 3101
        voice_client = None

        def __init__(self, channel):
            self.channel = channel

        def get_channel(self, channel_id):
            return self.channel if channel_id == self.channel.id else None

    async def scenario():
        agent = music.MusicAgent()
        channel = Channel()
        guild = Guild(channel)
        voice = Voice(guild, channel)
        agent.client.voice_clients = [voice]
        agent.client.get_guild = lambda value: guild if value == guild.id else None
        agent.client.get_channel = lambda value: channel if value == channel.id else None
        agent.states[guild.id] = music.GuildMusicState(guild_id=guild.id, voice_channel_id=channel.id)
        agent.log = lambda *args, **kwargs: None

        found, created = await agent._ensure_direct_voice_client(guild.id)
        assert found is voice
        assert created is False
        assert channel.connect_calls == 0

    run(scenario())
