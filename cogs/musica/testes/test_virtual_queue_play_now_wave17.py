from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.reproducao import controle_remoto
from cogs.musica.reproducao.fila_remota import agir_item_virtual_fila_worker
from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["play_now", "move", "remove"])
async def test_acao_virtual_envia_faixa_selecionada_sem_argumento_duplicado(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    source_url = "https://open.spotify.com/playlist/abc"
    selected = MusicTrack(
        title="Dominic Fike - Babydoll",
        webpage_url="https://open.spotify.com/track/0123456789ABCDEFGHIJKL",
        requester_id=10,
        uploader="Dominic Fike",
        source="Spotify público",
        extractor="metadata",
    )
    selected.virtual_playlist_instance_id = "playlist-1"
    selected.virtual_source_index = 87
    selected.virtual_provider = "spotify_public"
    selected.virtual_source_url = source_url
    current = MusicTrack(title="Her's - Harvey", webpage_url="https://example.test/current", requester_id=10)
    state = SimpleNamespace(current=current, last_voice_channel_id=30, last_text_channel_id=40)
    commands: list[tuple[str, dict]] = []
    synced: list[object] = []

    async def command(name: str, **kwargs):
        commands.append((name, kwargs))
        return {"ok": True, "state": {"status": "playing", "current": {"title": current.title}}}

    async def sync(_guild_id, fallback, _remote, **_kwargs):
        synced.append(fallback)

    monkeypatch.setattr(controle_remoto, "music_agent_command", command)
    router = SimpleNamespace(sync_music_agent_state=sync)
    result = await agir_item_virtual_fila_worker(
        router, 123, state, operation=action, track=selected,
        to_position=2 if action == "move" else None,
    )

    assert result["ok"] is True
    assert len(commands) == 1
    name, kwargs = commands[0]
    assert name == "queue_virtual_action"
    assert kwargs["virtual_action"] == action
    assert kwargs["instance_id"] == "playlist-1"
    assert kwargs["source_index"] == 87
    assert kwargs["track"]["title"] == selected.title
    assert kwargs["track"]["query"]
    assert synced == [None]


@pytest.mark.asyncio
async def test_worker_toca_ultimo_item_virtual_sem_perder_o_resto(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 123
    url = "https://open.spotify.com/playlist/abc"
    state = music.GuildMusicState(guild_id=gid)
    state.current = music.AgentTrack(title="Her's - Harvey", query="current")
    state.status = "playing"
    state.queue = [music.AgentTrack(title=f"ready-{index}", query=f"ready-{index}") for index in range(1, 25)]
    state.queue.append(music.AgentTrack(
        title="Spotify", transport_hint="playlist-cursor",
        virtual_playlist_cursor={
            "provider": "spotify_public", "source_url": url,
            "instance_id": "playlist-1", "next_offset": 25, "total_tracks": 88,
        },
    ))
    agent.states[gid] = state
    agent._guard_distinct_next_stream = lambda *args: False
    agent._next_resolving_prefetch_keys = lambda *args: set()

    async def play_next(_guild_id, *, preserve_current_to_history=True):
        state.current = state.queue.pop(0)

    agent._play_next = play_next
    result = await agent.cmd_queue_virtual_action({
        "guild_id": gid, "virtual_action": "play_now",
        "instance_id": "playlist-1", "provider": "spotify_public",
        "source_url": url, "source_index": 87,
        "track": {"title": "Dominic Fike - Babydoll", "query": "Dominic Fike Babydoll official audio"},
    })

    assert result["ok"] is True
    assert state.current.title == "Dominic Fike - Babydoll"
    assert agent._logical_queue_size(state) == 86
    markers = [item for item in state.queue if item.is_virtual_playlist_marker]
    assert len(markers) == 1
    assert agent._virtual_cursor_bounds(markers[0]) == (25, 87)

