"""Auditoria de início não pode mudar o áudio nem contar um falso pacote."""
import asyncio
import importlib
import json
import threading
from types import SimpleNamespace

from .test_music_agent_lifecycle import music


def test_trace_context_and_timings_are_bounded_and_never_copy_urls(music):
    track = music.AgentTrack()
    agent = music.MusicAgent()
    agent._attach_play_trace(track, {
        "trace_id": "trace", "start_context": {"entrypoint": "command", "helper_state": "cold", "url": "private",
                                                   "voice_state": "https://private.invalid/secret"},
        "controller_timing_ms": {"metadata_ms": 1.25, "huge": 10 ** 400, "bool": True, "infinite": float("inf"),
                                 "https://private.invalid": 1, "negative": -1},
    }, 123)
    assert track.start_context == {"entrypoint": "command", "helper_state": "cold"}
    assert track.controller_timing_ms == {"metadata_ms": 1.25}
    assert track.agent_received_monotonic == 123
    assert not track.start_attempt
    generated = music.AgentTrack()
    agent._attach_play_trace(generated, {}, 456)
    assert generated.trace_id and generated.trace_id != track.trace_id


def test_structured_start_events_have_only_allowlisted_categories_and_finite_times(music, monkeypatch):
    lines = []
    monkeypatch.setattr(music.MusicAgent, "_emit_log_line", staticmethod(lines.append))
    agent = music.MusicAgent()
    agent.log("music_start_outcome", guild_id=1, trace_id="trace", attempt_id="track:1", outcome="timeout",
              phase="resolve", start_context={"helper_state": "warming", "source_mode": "https://private.invalid"},
              controller_timing_ms={"metadata_ms": 10, "huge": 10 ** 400}, agent_timing_ms={"resolve_ms": float("nan")},
              query="private", token="secret")
    record = json.loads(lines[0].split("[music-start] ", 1)[1])
    assert record["event"] == "music_start_outcome" and record["origin"] == "agent"
    assert record["outcome"] == "timeout" and record["phase"] == "resolve"
    assert record["start_context"] == {"helper_state": "warming"}
    assert record["controller_timing_ms"] == {"metadata_ms": 10}
    assert record["agent_timing_ms"] == {}
    assert "private" not in lines[0] and "secret" not in lines[0]


def test_resolution_replacement_shares_attempt_but_not_mutable_public_context(music):
    agent = music.MusicAgent()
    previous = music.AgentTrack(trace_id="trace", start_context={"helper_state": "cold"},
                                agent_received_monotonic=1, start_attempt={"id": "track:1", "outcome": ""})
    following = music.AgentTrack()
    agent._copy_play_trace(previous, following)
    assert following.start_attempt is previous.start_attempt
    following.start_context["resolve_cache"] = "hit"
    assert "resolve_cache" not in previous.start_context
    assert "start_attempt" not in following.public() and "voice_preparation" not in following.public()
    assert following.public()["start_context"] == {"helper_state": "cold", "resolve_cache": "hit"}


def test_voice_timing_includes_adopted_lease_and_keeps_residual_wait_separate(music, monkeypatch):
    playback = importlib.import_module("cogs.musica.runtime_telefone.agente.reproducao")
    readings = iter((12.0, 14.0))
    monkeypatch.setattr(playback, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    agent = music.MusicAgent()
    voice = object()
    async def ready(guild_id):
        return voice, True
    agent._ensure_direct_voice_client = ready
    track = music.AgentTrack(voice_preparation=SimpleNamespace(started_monotonic=10.0, completed_monotonic=13.0))
    assert asyncio.run(agent._timed_voice_preconnect(1, track)) == (voice, True)
    assert track.agent_timing_ms == {"voice_preconnect_ms": 3000, "voice_preconnect_wait_ms": 2000}
    assert track.voice_preparation is None


def test_attempt_has_one_terminal_outcome_and_logging_failure_cannot_break_playback(music):
    agent = music.MusicAgent()
    track = music.AgentTrack(trace_id="trace", start_attempt={"id": "track:1", "outcome": "", "lock": threading.Lock()})
    barrier, events = threading.Barrier(3), []
    agent.log = lambda name, **kwargs: events.append((name, kwargs))
    def finish(outcome):
        barrier.wait()
        agent._finish_start_attempt(1, track, outcome)
    threads = [threading.Thread(target=finish, args=(outcome,)) for outcome in ("failed", "cancelled")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert len(events) == 1 and events[0][0] == "music_start_outcome"
    agent.log = lambda *args, **kwargs: (_ for _ in ()).throw(BrokenPipeError())
    agent._log_start_trace("music_start_attempt", 1, track)


def test_resolver_subspans_survive_lazy_track_replacement_until_packet(music):
    async def scenario():
        agent = music.MusicAgent()
        track = music.AgentTrack(query="song", trace_id="trace", start_context={"queue_state": "immediate"})
        state = music.GuildMusicState(guild_id=1, queue=[track])
        agent.states[1] = state
        events = []
        agent.log = lambda name, **kwargs: events.append((name, kwargs))
        async def resolve(*args, **kwargs):
            return music.AgentTrack(query="song", stream_url="https://media.invalid/song", transport_hint="direct-cache",
                                    agent_timing_ms={"resolve_slot_wait_ms": 7, "helper_init_ms": 12, "helper_extract_ms": 20})
        async def play(guild_id, resolved, **kwargs):
            trace = {"guild_id": guild_id, "track": resolved, "token": state.playback_token, "first_packet_monotonic": None}
            voice = SimpleNamespace(_music_packet_trace=trace)
            agent._record_first_music_packet(voice, trace)
        agent.resolve_track = resolve
        agent._play_direct_voice = play
        await agent._play_next(1)
        packet = next(data for name, data in events if name == "music_first_packet_sent")
        assert packet["agent_timing_ms"]["resolve_slot_wait_ms"] == 7
        assert packet["agent_timing_ms"]["helper_init_ms"] == 12
        assert packet["agent_timing_ms"]["helper_extract_ms"] == 20
        assert packet["start_context"]["resolve_cache"] == "hit"
        assert state.current.trace_id == "trace"
        assert [data["outcome"] for name, data in events if name == "music_start_outcome"] == ["success"]
    asyncio.run(scenario())


def test_failed_start_has_terminal_timeout_and_keeps_existing_failed_state(music):
    async def scenario():
        agent = music.MusicAgent()
        state = music.GuildMusicState(guild_id=1, queue=[music.AgentTrack(query="song", trace_id="trace")])
        agent.states[1] = state
        events = []
        agent.log = lambda name, **kwargs: events.append((name, kwargs))
        async def resolve(*args, **kwargs):
            raise TimeoutError("resolution deadline")
        agent.resolve_track = resolve
        await agent._play_next(1)
        assert state.status == "failed"
        assert len([name for name, _data in events if name == "music_start_attempt"]) == 1
        outcomes = [data for name, data in events if name == "music_start_outcome"]
        assert len(outcomes) == 1 and outcomes[0]["outcome"] == "timeout"
        assert outcomes[0]["phase"] == "resolve"
        assert not any(name == "music_first_packet_sent" for name, _data in events)
    asyncio.run(scenario())


def test_archive_fallback_packet_reports_online_source_and_keeps_initial_helper_state(music):
    async def scenario():
        agent = music.MusicAgent()
        original = music.AgentTrack(query="song", trace_id="fallback", archive_ref={"guild_id": 1},
                                    start_context={"queue_state": "immediate"})
        state = music.GuildMusicState(guild_id=1, queue=[original])
        agent.states[1] = state
        events = []
        agent.log = lambda name, **kwargs: events.append((name, kwargs))

        async def resolve(*args, **kwargs):
            agent._ytdlp_warm_client = SimpleNamespace(state="warm")
            return music.AgentTrack(query="song", stream_url="https://media.invalid/song", transport_hint="direct")

        async def play(guild_id, resolved, **kwargs):
            trace = {"guild_id": guild_id, "track": resolved, "token": state.playback_token,
                     "first_packet_monotonic": None}
            agent._record_first_music_packet(SimpleNamespace(_music_packet_trace=trace), trace)

        agent.resolve_track = resolve
        agent._play_direct_voice = play
        await agent._play_next(1)
        attempt = next(data for name, data in events if name == "music_start_attempt")
        packet = next(data for name, data in events if name == "music_first_packet_sent")
        assert attempt["start_context"]["source_mode"] == "archive"
        assert packet["start_context"]["source_mode"] == "online"
        assert packet["start_context"]["resolve_cache"] == "miss"
        assert attempt["start_context"]["helper_state"] == packet["start_context"]["helper_state"] == "cold"
        assert state.current.trace_id == "fallback"
    asyncio.run(scenario())
