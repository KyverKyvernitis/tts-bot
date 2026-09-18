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
    from music_agent_runtime.lifecycle import playback_owned

    current = object()
    state = types.SimpleNamespace(current=current, playback_token=7)
    assert playback_owned(state, current, 7) is True
    assert playback_owned(state, object(), 7) is False
    assert playback_owned(state, current, 8) is False


def test_lifecycle_cancel_tasks_deduplicates_and_collects_cancellation(music):
    from music_agent_runtime.lifecycle import cancel_tasks

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
