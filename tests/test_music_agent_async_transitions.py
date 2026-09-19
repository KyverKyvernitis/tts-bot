import asyncio
import base64
import types

import pytest

from test_music_agent_lifecycle import _load_music_agent


@pytest.fixture
def music(monkeypatch):
    return _load_music_agent(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def test_resolved_http_track_always_uses_direct_voice(music):
    agent = music.MusicAgent()
    assert agent._should_use_direct_voice(
        music.AgentTrack(title="x", query="x", stream_url="https://media.example/audio")
    ) is True


def test_track_without_resolved_stream_is_not_sent_to_player(music):
    agent = music.MusicAgent()
    assert agent._should_use_direct_voice(
        music.AgentTrack(title="x", query="x", stream_url="")
    ) is False


def test_status_declares_direct_discord_voice_backend_and_no_lavalink_fields(music):
    agent = music.MusicAgent()
    payload = agent.status_payload()
    assert payload["playback_backend"] == "discord-voice-direct"
    assert "lavalink_uri" not in payload
    assert "lavalink_node" not in payload
    assert "pool_connected" not in payload
    assert "wavelink" not in payload["voice_dependencies"]


def test_music_health_treats_tts_providers_as_optional(music, monkeypatch):
    agent = music.MusicAgent()

    real_import = music.importlib.import_module

    def fake_import(name):
        if name in {"gtts", "edge_tts"}:
            raise ModuleNotFoundError(name)
        return real_import(name)

    monkeypatch.setattr(music.importlib, "import_module", fake_import)
    payload = agent.voice_dependencies_payload()

    assert "gTTS" not in payload["missing"]
    assert "edge-tts" not in payload["missing"]
    assert set(payload["optional_missing"]) == {"gTTS", "edge-tts"}
    assert payload["checks"]["gTTS"]["optional"] is True
    assert payload["checks"]["edge-tts"]["optional"] is True


def test_run_cleans_http_runner_when_client_start_returns(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        agent.discord_token = "token"
        events = []

        class Runner:
            def __init__(self, app):
                events.append("runner")
            async def setup(self):
                events.append("setup")
            async def cleanup(self):
                events.append("cleanup")

        class Site:
            def __init__(self, runner, host, port):
                pass
            async def start(self):
                events.append("site")

        async def client_start(token):
            events.append("client")

        monkeypatch.setattr(music.web, "AppRunner", Runner)
        monkeypatch.setattr(music.web, "TCPSite", Site)
        agent.client.start = client_start
        await agent.run()
        assert events == ["runner", "setup", "site", "client", "cleanup"]

    run(scenario())


def test_shutdown_cancels_background_tasks_disconnects_players_and_closes_resources(music):
    async def scenario():
        agent = music.MusicAgent()
        cancelled = []
        gate = asyncio.Event()

        async def blocker(name):
            try:
                await gate.wait()
            finally:
                cancelled.append(name)

        idle = asyncio.create_task(blocker("idle"))
        prefetch = asyncio.create_task(blocker("prefetch"))
        tts = asyncio.create_task(blocker("tts"))
        agent._idle_disconnect_tasks[1] = idle
        agent._prefetch_tasks["1:x"] = prefetch
        agent._active_tts_requests = {(1, "tts"): tts}
        agent._cancelled_tts_requests = {}
        await asyncio.sleep(0)

        class Player:
            def __init__(self):
                self.disconnected = 0
            def is_playing(self):
                return True
            def is_paused(self):
                return False
            def stop(self):
                raise RuntimeError("stop failure must not block disconnect")
            async def disconnect(self, force=False):
                self.disconnected += 1

        first = Player()
        second = Player()
        agent.states[1] = music.GuildMusicState(guild_id=1, player=first, status="playing")
        agent.states[2] = music.GuildMusicState(guild_id=2, player=second, status="playing")

        closed = []
        cleaned = []
        async def close():
            closed.append(True)
        class Runner:
            async def cleanup(self):
                cleaned.append(True)
        agent.client.close = close
        agent._runner = Runner()

        await agent.shutdown()
        await agent.shutdown()
        await asyncio.sleep(0)

        assert set(cancelled) == {"idle", "prefetch", "tts"}
        assert all(task.done() for task in (idle, prefetch, tts))
        assert first.disconnected == second.disconnected == 1
        assert all(st.player is None for st in agent.states.values())
        assert closed == [True]
        assert cleaned == [True]
        assert agent._idle_disconnect_tasks == {}
        assert agent._prefetch_tasks == {}
        assert agent._active_tts_requests == {}

    run(scenario())


def test_direct_tts_cancellation_restores_idle_state_and_idle_timer(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 203
        channel_id = 902
        played = asyncio.Event()
        stopped = []
        idle = []

        class Audio:
            def __init__(self, *args, **kwargs):
                self.cleaned = False
            def cleanup(self):
                self.cleaned = True
            def is_opus(self):
                return False
            def read(self):
                return b""

        monkeypatch.setattr(music.discord, "FFmpegPCMAudio", Audio)

        class VoiceClient:
            def __init__(self):
                self.channel = types.SimpleNamespace(id=channel_id)
                self.playing = False
            def is_connected(self):
                return True
            def is_playing(self):
                return self.playing
            def is_paused(self):
                return False
            def play(self, source, after=None):
                self.playing = True
                played.set()
            def stop(self):
                self.playing = False
                stopped.append(True)
            async def move_to(self, channel):
                self.channel = channel
            async def disconnect(self, force=False):
                return None

        voice = VoiceClient()
        guild = types.SimpleNamespace(voice_client=voice)
        channel = types.SimpleNamespace(id=channel_id)

        async def resolve(*args, **kwargs):
            return guild, channel

        agent._resolve_guild_and_channel = resolve
        agent._schedule_idle_disconnect = lambda value: idle.append(value)
        body = {
            "guild_id": gid,
            "voice_channel_id": channel_id,
            "tts_request_id": "cancel-me",
            "audio_b64": base64.b64encode(b"fake-audio").decode("ascii"),
        }
        task = asyncio.create_task(agent._run_tts_request(body, direct=True))
        await played.wait()
        response = await agent.cmd_cancel_tts({"guild_id": gid, "tts_request_id": "cancel-me"})
        assert response["cancelled"] is True
        assert task.cancelled()
        st = agent.states[gid]
        assert st.status == "idle"
        assert idle == [gid]
        assert stopped

    run(scenario())


def test_concurrent_overlay_tts_keeps_ducked_until_last_overlay_finishes(music, monkeypatch):
    async def scenario():
        agent = music.MusicAgent()
        gid = 204

        class Audio:
            def __init__(self, *args, **kwargs):
                self.cleaned = False
            def cleanup(self):
                self.cleaned = True
            def is_opus(self):
                return False
            def read(self):
                return b""

        monkeypatch.setattr(music.discord, "FFmpegPCMAudio", Audio)

        class Mixer:
            def __init__(self):
                self.futures = []
            def add_tts(self, source, *, volume=1.0):
                fut = asyncio.get_running_loop().create_future()
                self.futures.append(fut)
                return fut
            def cancel_tts(self, future):
                if future in self.futures:
                    self.futures.remove(future)
                if not future.done():
                    future.cancel()
            def has_tts(self):
                return bool(self.futures)

        monkeypatch.setattr(music, "AgentMixedAudioSource", Mixer)
        mixer = Mixer()
        player = types.SimpleNamespace(source=mixer)
        st = music.GuildMusicState(guild_id=gid, current=music.AgentTrack(title="music", query="music"), player=player)
        agent.states[gid] = st
        payload = base64.b64encode(b"fake-audio").decode("ascii")
        one = asyncio.create_task(agent.cmd_tts({"guild_id": gid, "audio_b64": payload}))
        two = asyncio.create_task(agent.cmd_tts({"guild_id": gid, "audio_b64": payload}))
        while len(mixer.futures) < 2:
            await asyncio.sleep(0)
        first, second = list(mixer.futures)
        first.set_result(None)
        for _ in range(10):
            if one.done() or two.done():
                break
            await asyncio.sleep(0)
        assert one.done() ^ two.done()
        assert st.ducked is True
        second.set_result(None)
        await asyncio.gather(one, two)
        assert st.ducked is False

    run(scenario())


def test_lifecycle_playback_owned_requires_track_identity_and_generation(music):
    from cogs.musica.runtime_telefone.agente.ciclo_vida import playback_owned

    current = object()
    state = types.SimpleNamespace(current=current, playback_token=7)
    assert playback_owned(state, current, 7) is True
    assert playback_owned(state, object(), 7) is False
    assert playback_owned(state, current, 8) is False


def test_lifecycle_cancel_tasks_deduplicates_and_collects_cancellation(music):
    from cogs.musica.runtime_telefone.agente.ciclo_vida import cancel_tasks

    async def scenario():
        entered = asyncio.Event()
        cleaned = []

        async def blocker():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        task = asyncio.create_task(blocker())
        await entered.wait()
        assert await cancel_tasks([task, task, None]) == 1
        assert task.done()
        assert cleaned == [True]

    run(scenario())


def test_amain_natural_agent_exit_runs_shutdown_and_collects_tasks(music, monkeypatch):
    async def scenario():
        events = []

        class Agent:
            async def run(self):
                events.append("run")
            async def shutdown(self):
                events.append("shutdown")

        agent = Agent()
        monkeypatch.setattr(music, "MusicAgent", lambda: agent)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *args, **kwargs: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda *args, **kwargs: True)
        await music.amain()
        assert events == ["run", "shutdown"]

    run(scenario())


def test_compact_status_skips_dependency_probe_and_filters_guild(music, monkeypatch):
    agent = music.MusicAgent()
    agent.states[7] = music.GuildMusicState(guild_id=7, status="playing")
    agent.states[8] = music.GuildMusicState(guild_id=8, status="idle")

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("status compacto não deve sondar dependências")

    monkeypatch.setattr(agent, "voice_dependencies_payload", forbidden_probe)
    payload = agent.status_payload(guild_id=7, compact=True)

    assert payload["ok"] is True
    assert payload["playback_backend"] == "discord-voice-direct"
    assert set(payload["guilds"]) == {"7"}
    assert "voice_dependencies" not in payload
    assert "cache" not in payload


def test_dependency_probe_is_cached_between_full_health_calls(music, monkeypatch):
    agent = music.MusicAgent()
    agent._voice_dependencies_cache_ttl = 60.0
    calls = []
    real_import = music.importlib.import_module

    def counted_import(name):
        calls.append(name)
        return real_import(name)

    monkeypatch.setattr(music.importlib, "import_module", counted_import)
    first = agent.voice_dependencies_payload()
    first_calls = len(calls)
    second = agent.voice_dependencies_payload()

    assert first_calls > 0
    assert len(calls) == first_calls
    assert second == first
    assert second is not first
    assert second["checks"] is not first["checks"]


def test_concurrent_resolve_coalesces_and_releases_lock_registry(music):
    async def scenario():
        agent = music.MusicAgent()
        calls = []

        def resolve_once(query):
            calls.append(query)
            music.time.sleep(0.05)
            return {
                "title": "same",
                "stream_url": "https://media.example/audio",
                "webpage_url": "https://example.test/same",
            }

        agent._resolve_with_ytdlp = resolve_once
        body = {"guild_id": 41}
        meta = {"title": "same", "webpage_url": "https://example.test/same"}
        one, two = await asyncio.gather(
            agent.resolve_track("https://example.test/same", track_meta=dict(meta), body=body),
            agent.resolve_track("https://example.test/same", track_meta=dict(meta), body=body),
        )

        assert one.stream_url == two.stream_url == "https://media.example/audio"
        assert len(calls) == 1
        assert agent._resolve_locks == {}
        assert agent._resolve_lock_users == {}

    run(scenario())


def test_ephemeral_tts_lock_registry_is_released(music):
    async def scenario():
        agent = music.MusicAgent()
        async with agent._registry_lock(agent._tts_direct_locks, agent._tts_direct_lock_users, 91):
            assert 91 in agent._tts_direct_locks
            assert agent._tts_direct_lock_users[91] == 1
        assert agent._tts_direct_locks == {}
        assert agent._tts_direct_lock_users == {}

    run(scenario())



def test_mixer_hot_path_uses_frame_level_scaling(music):
    class Source:
        def __init__(self, frame):
            self.frame = frame
            self.cleaned = False
        def read(self):
            frame, self.frame = self.frame, b""
            return frame
        def cleanup(self):
            self.cleaned = True

    class FastAudio:
        def __init__(self):
            self.mul_calls = []
        def mul(self, frame, width, volume):
            self.mul_calls.append((len(frame), width, volume))
            return b"x" * len(frame)

    async def scenario():
        source = Source(b"\x01\x00" * 1920)
        mixer = music.AgentMixedAudioSource(
            loop=asyncio.get_running_loop(),
            music_source=source,
            music_volume=0.55,
        )
        fast = FastAudio()
        mixer._audioop_module = fast
        mixer._samples = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("hot path caiu no loop Python"))
        frame = mixer.read()
        assert frame == b"x" * 3840
        assert fast.mul_calls == [(3840, 2, 0.55)]

    run(scenario())


def test_resolve_scheduler_prioritizes_interactive_over_prefetch(music):
    async def scenario():
        agent = music.MusicAgent()
        agent.resolve_max_concurrency = 1
        entered = []
        release_holder = asyncio.Event()

        async def holder():
            async with agent._resolve_slot(0):
                entered.append("holder")
                await release_holder.wait()

        async def waiter(name, priority):
            async with agent._resolve_slot(priority):
                entered.append(name)

        first = asyncio.create_task(holder())
        await asyncio.sleep(0)
        prefetch = asyncio.create_task(waiter("prefetch", 20))
        await asyncio.sleep(0)
        interactive = asyncio.create_task(waiter("interactive", 0))
        await asyncio.sleep(0)
        release_holder.set()
        await asyncio.gather(first, prefetch, interactive)

        assert entered == ["holder", "interactive", "prefetch"]
        assert agent._resolve_active == 0
        assert agent._resolve_waiters == []

    run(scenario())


def test_cancelled_resolution_signals_blocking_resolver(music):
    async def scenario():
        agent = music.MusicAgent()
        started = music.threading.Event()
        stopped = music.threading.Event()

        def blocking_resolve(query):
            cancel_event = agent._resolve_thread_local.cancel_event
            started.set()
            while not cancel_event.wait(0.01):
                pass
            stopped.set()
            raise RuntimeError("cancelled")

        agent._resolve_with_ytdlp = blocking_resolve
        task = asyncio.create_task(
            agent.resolve_track(
                "https://example.test/cancel",
                track_meta={"title": "cancel", "webpage_url": "https://example.test/cancel"},
                body={"guild_id": 99},
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert agent._resolve_active == 0

    run(scenario())



def test_cancelled_resolve_waiter_does_not_leak_scheduler_slot(music):
    async def scenario():
        agent = music.MusicAgent()
        agent.resolve_max_concurrency = 1
        holder_release = asyncio.Event()

        async def holder():
            async with agent._resolve_slot(0):
                await holder_release.wait()

        first = asyncio.create_task(holder())
        await asyncio.sleep(0)
        waiting = asyncio.create_task(agent._acquire_resolve_slot(20))
        await asyncio.sleep(0)
        assert len(agent._resolve_waiters) == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert agent._resolve_waiters == []
        holder_release.set()
        await first
        assert agent._resolve_active == 0

    run(scenario())


def test_state_revision_ignores_volatile_playback_position(music):
    class Player:
        connected = True
        playing = True
        position = 1000

        def is_connected(self):
            return True

        def is_playing(self):
            return True

    player = Player()
    state = music.GuildMusicState(
        guild_id=9,
        current=music.AgentTrack(title="Faixa", query="q"),
        status="playing",
        player=player,
        updated_at=123.456789,
        playback_token=7,
    )

    first = state.public()
    player.position = 9000
    second = state.public()

    assert first["position_ms"] != second["position_ms"]
    assert first["state_revision"] == second["state_revision"]

    state.updated_at += 0.001
    assert state.public()["state_revision"] != first["state_revision"]


def test_compact_status_can_return_unchanged_without_serializing_guild(music):
    agent = music.MusicAgent()
    state = music.GuildMusicState(
        guild_id=91,
        current=music.AgentTrack(title="Faixa", query="q"),
        status="playing",
        updated_at=222.25,
        playback_token=4,
    )
    agent.states[91] = state
    revision = state.state_revision()

    payload = agent.status_payload(guild_id=91, compact=True, known_revision=revision)

    assert payload["unchanged"] is True
    assert payload["state_revision"] == revision
    assert payload["guilds"] == {}
