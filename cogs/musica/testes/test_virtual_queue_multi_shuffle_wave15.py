from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack, PlaylistCursor
from cogs.musica.reproducao.playlist_virtual import carregar_pagina_fila_virtual, payload_cursor_playlist
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.testes.runtime_telefone.test_music_agent_lifecycle import _load_music_agent


def _track(title: str, queue_id: str = "") -> MusicTrack:
    return MusicTrack(
        title=title,
        webpage_url=f"https://example.test/{title}",
        requester_id=1,
        requester_name="tester",
        duration=180,
        uploader="artist",
        source="Spotify",
        extractor="metadata",
        queue_item_id=queue_id,
    )


def _agent_track(title: str) -> AgentTrack:
    return AgentTrack(title=title, query=title, webpage_url=f"https://example.test/{title}")


def _marker(instance: str, source: str, start: int, total: int, *, block_end: int | None = None) -> AgentTrack:
    cursor = {
        "provider": "spotify_public",
        "source_url": source,
        "title": f"Playlist {instance}",
        "resource_type": "playlist",
        "resource_id": instance,
        "instance_id": instance,
        "next_offset": start,
        "total_tracks": total,
        "exhausted": False,
    }
    if block_end is not None:
        cursor["block_end_offset"] = block_end
    return AgentTrack(
        title=f"Playlist {instance}",
        source="playlist-virtual",
        transport_hint="playlist-cursor",
        virtual_playlist_cursor=cursor,
    )


def _logical_titles(agent, state: GuildMusicState) -> list[str]:
    result: list[str] = []
    logical = 1
    total = agent._logical_queue_size(state)
    for position in range(1, total + 1):
        physical = agent._physical_track_index_at_logical_position(state, position)
        if physical is not None:
            result.append(state.queue[physical].title)
        else:
            result.append("<virtual>")
        logical += 1
    return result


def test_playlist_cursor_preserva_instancia_e_limite_de_bloco() -> None:
    cursor = PlaylistCursor(
        provider="spotify_public",
        source_url="https://open.spotify.com/playlist/abc",
        next_offset=25,
        total_tracks=87,
        instance_id="instance-a",
        block_end_offset=50,
    )
    advanced = cursor.advanced(10)
    assert advanced.instance_id == "instance-a"
    assert advanced.block_end_offset == 50
    assert advanced.public()["instance_id"] == "instance-a"
    assert advanced.public()["block_end_offset"] == 50


def test_payload_cursor_gera_instancia_distinta_para_duas_insercoes_iguais() -> None:
    url = "https://open.spotify.com/playlist/abc"
    first = PlaylistCursor(provider="spotify_public", source_url=url, next_offset=25, total_tracks=87)
    second = PlaylistCursor(provider="spotify_public", source_url=url, next_offset=25, total_tracks=87)
    a = payload_cursor_playlist(first)
    b = payload_cursor_playlist(second)
    assert a["virtual_playlist_cursor"]["instance_id"]
    assert b["virtual_playlist_cursor"]["instance_id"]
    assert a["virtual_playlist_cursor"]["instance_id"] != b["virtual_playlist_cursor"]["instance_id"]


def test_estado_publico_expoe_multiplas_playlists_e_total_logico() -> None:
    st = GuildMusicState(guild_id=1)
    st.queue = [
        _agent_track("A-ready-1"),
        _agent_track("A-ready-2"),
        _marker("A", "https://open.spotify.com/playlist/a", 25, 87),
        _agent_track("B-ready-1"),
        _agent_track("B-ready-2"),
        _marker("B", "https://open.spotify.com/playlist/b", 25, 60),
    ]
    public = st.public()
    assert public["queue_size"] == 4
    assert public["logical_queue_size"] == 4 + (87 - 25) + (60 - 25)
    assert len(public["virtual_playlists"]) == 2
    assert [entry["kind"] for entry in public["queue_layout"]] == [
        "track", "track", "virtual", "track", "track", "virtual"
    ]
    assert public["virtual_playlist"]["cursor"]["instance_id"] == "A"


@pytest.mark.asyncio
async def test_pagina_logica_atravessa_duas_playlists_sem_materializar_tudo() -> None:
    state = MusicGuildState()
    state.agent_virtual_playlists = [
        {
            "active": True,
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/a",
            "title": "A",
            "instance_id": "A",
            "next_offset": 25,
            "total_tracks": 30,
            "block_end_offset": 30,
            "requester_id": 1,
            "requester_name": "tester",
            "remaining": 5,
        },
        {
            "active": True,
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/b",
            "title": "B",
            "instance_id": "B",
            "next_offset": 40,
            "total_tracks": 45,
            "block_end_offset": 45,
            "requester_id": 1,
            "requester_name": "tester",
            "remaining": 5,
        },
    ]
    state.agent_virtual_playlist = state.agent_virtual_playlists[0]
    state.agent_virtual_playlist_browse_key = "layout-v1"
    state.agent_queue_layout = [
        {"kind": "track", "track": _track("ready-1", "r1")},
        {"kind": "virtual", "virtual": state.agent_virtual_playlists[0]},
        {"kind": "track", "track": _track("ready-2", "r2")},
        {"kind": "virtual", "virtual": state.agent_virtual_playlists[1]},
    ]
    calls: list[tuple[str, int, int]] = []

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            calls.append((cursor.instance_id, cursor.next_offset, limit))
            tracks = [_track(f"{cursor.instance_id}-{i}") for i in range(cursor.next_offset, cursor.next_offset + limit)]
            return ExtractedBatch(tracks=tracks, query=cursor.source_url, is_playlist=True)

    router = SimpleNamespace(get_state=lambda _gid: state, extractor=Extractor())
    page = await carregar_pagina_fila_virtual(router, 1, 0, page_size=8)

    assert [item.title for item in page] == [
        "ready-1", "A-25", "A-26", "A-27", "A-28", "A-29", "ready-2", "B-40"
    ]
    assert calls == [("A", 25, 5), ("B", 40, 1)]
    assert page[1].virtual_playlist_instance_id == "A"
    assert page[1].virtual_source_index == 25
    assert page[-1].virtual_playlist_instance_id == "B"
    assert page[-1].virtual_source_index == 40


@pytest.mark.asyncio
async def test_shuffle_global_inclui_prontas_e_blocos_de_todas_as_playlists(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 99
    st = music.GuildMusicState(guild_id=gid)
    ready = [_agent_track(f"ready-{i}") for i in range(1, 8)]
    ready_ids = {item.queue_item_id for item in ready}
    st.queue = ready[:4] + [
        _marker("A", "https://open.spotify.com/playlist/a", 25, 87),
    ] + ready[4:] + [
        _marker("B", "https://open.spotify.com/playlist/b", 25, 60),
    ]
    agent.states[gid] = st

    before = st._logical_queue_size()
    result = await agent.cmd_shuffle({"guild_id": gid})
    after = st._logical_queue_size()

    assert result["ok"] is True and result["shuffled"] is True
    assert before == after == 7 + 62 + 35
    assert {item.queue_item_id for item in st.queue if not item.is_virtual_playlist_marker} == ready_ids
    markers = [item for item in st.queue if item.is_virtual_playlist_marker]
    assert markers
    assert {m.virtual_playlist_cursor.get("instance_id") for m in markers} == {"A", "B"}
    assert all(m.virtual_playlist_cursor.get("block_end_offset") not in (None, "") for m in markers)
    assert all(int(m.virtual_playlist_cursor.get("shuffle_seed") or 0) > 0 for m in markers)
    assert sum(agent._virtual_cursor_bounds(m)[1] - agent._virtual_cursor_bounds(m)[0] for m in markers) == 97
    assert st.virtual_shuffle_active is False
    assert st.virtual_shuffle_seed == 0


@pytest.mark.asyncio
async def test_virtual_remove_splita_cursor_sem_duplicar(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 100
    st = music.GuildMusicState(guild_id=gid)
    st.queue = [_marker("A", "https://open.spotify.com/playlist/a", 25, 30)]
    agent.states[gid] = st

    result = await agent.cmd_queue_virtual_action({
        "guild_id": gid,
        "virtual_action": "remove",
        "instance_id": "A",
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/a",
        "source_index": 27,
        "track": {"title": "removed", "webpage_url": "https://example.test/removed"},
    })

    assert result["ok"] is True
    bounds = [agent._virtual_cursor_bounds(item) for item in st.queue if item.is_virtual_playlist_marker]
    assert bounds == [(25, 27), (28, 30)]
    assert agent._logical_queue_size(st) == 4


@pytest.mark.asyncio
async def test_virtual_move_materializa_so_item_escolhido_e_insere_em_posicao_logica(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 101
    st = music.GuildMusicState(guild_id=gid)
    st.queue = [
        _agent_track("ready"),
        _marker("A", "https://open.spotify.com/playlist/a", 25, 30),
        _marker("B", "https://open.spotify.com/playlist/b", 40, 45),
    ]
    agent.states[gid] = st

    result = await agent.cmd_queue_virtual_action({
        "guild_id": gid,
        "virtual_action": "move",
        "instance_id": "A",
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/a",
        "source_index": 27,
        "to_position": 8,
        "track": {
            "title": "A-27",
            "webpage_url": "https://example.test/A-27",
            "query": "A-27",
        },
    })

    assert result["ok"] is True
    assert agent._logical_queue_size(st) == 11
    physical = agent._physical_track_index_at_logical_position(st, 8)
    assert physical is not None
    assert st.queue[physical].title == "A-27"
    # Índice original foi excluído do segmento A; só existe a faixa materializada movida.
    assert sum(1 for item in st.queue if not item.is_virtual_playlist_marker and item.title == "A-27") == 1


def test_ui_wave15_tem_modal_de_pagina_selecao_virtual_e_sem_notas_tecnicas() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "interface" / "componentes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    queue_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueueView")
    refresh = next(node for node in queue_cls.body if isinstance(node, ast.FunctionDef) and node.name == "_refresh_components")
    refresh_source = ast.get_source_segment(source, refresh) or ""
    jump = next(node for node in queue_cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "jump_page")
    jump_source = ast.get_source_segment(source, jump) or ""

    assert "page_label.callback = self.jump_page" in refresh_source
    assert "JumpQueuePageModal" in jump_source
    assert "if page_items:" in refresh_source
    assert "virtual_queue_action" in source
    assert "Esta página é visualização da playlist virtual" not in source
    assert "metadata carregada sob demanda" not in source

@pytest.mark.asyncio
async def test_virtual_move_invalido_nao_remove_item_original(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 102
    st = music.GuildMusicState(guild_id=gid)
    st.queue = [_marker("A", "https://open.spotify.com/playlist/a", 25, 30)]
    agent.states[gid] = st
    before = [item.public() for item in st.queue]

    result = await agent.cmd_queue_virtual_action({
        "guild_id": gid,
        "virtual_action": "move",
        "instance_id": "A",
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/a",
        "source_index": 27,
        "to_position": 999,
        "track": {"title": "A-27", "webpage_url": "https://example.test/A-27", "query": "A-27"},
    })

    assert result["ok"] is False
    assert [item.public() for item in st.queue] == before
    assert agent._logical_queue_size(st) == 5


@pytest.mark.asyncio
async def test_acao_virtual_desconhecida_nao_muta_fila(monkeypatch: pytest.MonkeyPatch) -> None:
    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 103
    st = music.GuildMusicState(guild_id=gid)
    st.queue = [_marker("A", "https://open.spotify.com/playlist/a", 25, 30)]
    agent.states[gid] = st
    before = [item.public() for item in st.queue]

    result = await agent.cmd_queue_virtual_action({
        "guild_id": gid,
        "virtual_action": "explode",
        "instance_id": "A",
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/a",
        "source_index": 27,
        "track": {"title": "A-27", "webpage_url": "https://example.test/A-27", "query": "A-27"},
    })

    assert result["ok"] is False
    assert [item.public() for item in st.queue] == before
    assert agent._logical_queue_size(st) == 5


@pytest.mark.asyncio
async def test_pagina_virtual_respeita_mesma_ordem_shuffle_do_bloco() -> None:
    import random

    state = MusicGuildState()
    info = {
        "active": True,
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/a",
        "title": "A",
        "instance_id": "A",
        "next_offset": 25,
        "total_tracks": 30,
        "block_end_offset": 30,
        "shuffle_seed": 12345,
        "requester_id": 1,
        "requester_name": "tester",
        "remaining": 5,
    }
    state.agent_virtual_playlists = [info]
    state.agent_virtual_playlist = info
    state.agent_virtual_playlist_browse_key = "shuffle-layout"
    state.agent_queue_layout = [{"kind": "virtual", "virtual": info}]

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            tracks = [_track(f"A-{i}") for i in range(cursor.next_offset, cursor.next_offset + limit)]
            return ExtractedBatch(tracks=tracks, query=cursor.source_url, is_playlist=True)

    router = SimpleNamespace(get_state=lambda _gid: state, extractor=Extractor())
    page = await carregar_pagina_fila_virtual(router, 1, 0, page_size=5)

    expected = list(range(25, 30))
    random.Random(12345).shuffle(expected)
    assert [item.virtual_source_index for item in page] == expected
    assert [item.title for item in page] == [f"A-{index}" for index in expected]


@pytest.mark.asyncio
async def test_refill_de_bloco_aplica_shuffle_deterministico_sem_materializar_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    music = _load_music_agent(monkeypatch)
    agent = music.MusicAgent()
    gid = 104
    ready = _agent_track("ready")
    marker = _marker("A", "https://open.spotify.com/playlist/a", 25, 30, block_end=30)
    marker.virtual_playlist_cursor["shuffle_seed"] = 6789
    st = music.GuildMusicState(guild_id=gid, queue=[ready, marker])
    agent.states[gid] = st
    agent._schedule_next_queue_prefetch = lambda *a, **k: None

    raw = [
        {"title": f"A-{i}", "query": f"A-{i}", "webpage_url": f"https://example.test/A-{i}"}
        for i in range(25, 30)
    ]
    result = await agent.cmd_playlist_refill({
        "guild_id": gid,
        "expected_cursor": dict(marker.virtual_playlist_cursor),
        "next_cursor": {
            **dict(marker.virtual_playlist_cursor),
            "next_offset": 30,
            "exhausted": True,
        },
        "tracks": raw,
    })

    expected = [f"A-{i}" for i in range(25, 30)]
    random.Random(6789).shuffle(expected)
    assert result["ok"] is True
    assert [item.title for item in st.queue[1:]] == expected
    assert agent._logical_queue_size(st) == 6
