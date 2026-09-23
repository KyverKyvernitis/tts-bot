from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone.monitor import _assinatura_painel_remoto
from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.nucleo.modelos import ExtractedBatch, MusicTrack, PlaylistCursor
from cogs.musica.nucleo.playlist_virtual import logical_virtual_queue_count
from cogs.musica.reproducao.playlist_virtual import carregar_pagina_fila_virtual
from cogs.musica.reproducao.sincronizacao import sincronizar_fila_remota
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "interface" / "componentes.py"


def _class_method_source(class_name: str, method_name: str) -> str:
    source = COMPONENTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    method = next(
        item
        for item in cls.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name
    )
    return ast.get_source_segment(source, method) or ""


def _virtual_remote(*, total: int = 87, next_offset: int = 25, materialized_before: int = 24) -> dict:
    return {
        "cursor": {
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/abc1234567890123456789",
            "title": "Playlist Grande",
            "resource_type": "playlist",
            "resource_id": "abc1234567890123456789",
            "next_offset": next_offset,
            "total_tracks": total,
            "exhausted": False,
        },
        "materialized_before": materialized_before,
        "waiting": False,
    }


def _track(number: int) -> MusicTrack:
    return MusicTrack(
        title=f"Faixa {number}",
        webpage_url=f"https://example.invalid/{number}",
        requester_id=1,
        duration=180,
        uploader="Artista",
        source="Spotify público",
    )


def test_total_logico_remove_current_da_playlist_sem_materializar_tudo() -> None:
    # 25 itens já foram materializados. Um deles virou current, logo há 86
    # faixas ainda na fila lógica, embora somente 24 estejam prontas no Worker.
    assert logical_virtual_queue_count(
        total_tracks=87,
        next_offset=25,
        materialized_before=24,
        remote_queue_size=24,
    ) == 86


def test_queueview_pagina_pelo_total_logico_e_refresca_worker_antes_de_redesenhar() -> None:
    max_page = _class_method_source("QueueView", "_max_page")
    prepare = _class_method_source("QueueView", "_prepare_page")
    redraw = _class_method_source("QueueView", "_redraw")

    assert "_queue_total_count(state, items)" in max_page
    assert "refresh_queue_controller" in prepare
    assert "await refresh(self.guild_id)" in prepare
    assert "carregar_pagina_fila_virtual" in prepare
    assert "await interaction.response.defer()" in redraw


@pytest.mark.asyncio
async def test_pagina_distante_da_playlist_e_metadata_sob_demanda_sem_mover_cursor_do_worker() -> None:
    state = MusicGuildState()
    state.agent_virtual_playlist = {
        "active": True,
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/abc1234567890123456789",
        "title": "Playlist Grande",
        "resource_type": "playlist",
        "resource_id": "abc1234567890123456789",
        "next_offset": 25,
        "total_tracks": 87,
        "materialized_before": 24,
        "waiting": False,
    }
    seen: list[PlaylistCursor] = []

    class Extractor:
        async def continue_playlist_window(self, cursor, *, requester_id, requester_name, limit):
            seen.append(cursor)
            # página 4 (0-based 3), com 8 itens por página: logical_start=24;
            # consumed=1, portanto metadata deve começar no offset absoluto 25.
            tracks = [_track(number) for number in range(cursor.next_offset + 1, cursor.next_offset + limit + 1)]
            return ExtractedBatch(
                tracks=tracks,
                query=cursor.source_url,
                is_playlist=True,
                playlist_title=cursor.title,
                playlist_cursor=PlaylistCursor(
                    provider=cursor.provider,
                    source_url=cursor.source_url,
                    title=cursor.title,
                    resource_type=cursor.resource_type,
                    resource_id=cursor.resource_id,
                    next_offset=cursor.next_offset + len(tracks),
                    total_tracks=cursor.total_tracks,
                    exhausted=False,
                ),
            )

    router = SimpleNamespace(get_state=lambda guild_id: state, extractor=Extractor())

    page = await carregar_pagina_fila_virtual(router, 123, 3, page_size=8)

    assert len(page) == 8
    assert [item.title for item in page[:2]] == ["Faixa 26", "Faixa 27"]
    assert len(seen) == 1
    assert seen[0].next_offset == 25
    # Browse/UI não altera o cursor autoritativo do Worker.
    assert state.agent_virtual_playlist["next_offset"] == 25
    assert state.agent_virtual_playlist_pages[3] == page

    # Segunda abertura da mesma página deve ser local e não repetir I/O.
    again = await carregar_pagina_fila_virtual(router, 123, 3, page_size=8)
    assert again == page
    assert len(seen) == 1


def test_sync_invalida_paginas_de_browse_quando_current_avanca() -> None:
    state = MusicGuildState()
    state.agent_virtual_playlist_pages[3] = [_track(26)]
    state.agent_virtual_playlist_browse_key = (
        "spotify_public|https://open.spotify.com/playlist/abc1234567890123456789|1|87"
    )

    sincronizar_fila_remota(
        state,
        {
            "current": {"title": "Faixa 2", "queue_item_id": "current-2"},
            "queue": [],
            "queue_size": 23,
            "virtual_playlist": _virtual_remote(next_offset=25, materialized_before=23),
        },
        limite_fila=100,
        limite_historico=25,
    )

    assert state.agent_virtual_playlist_pages == {}
    assert state.agent_virtual_playlist_browse_key.endswith("|2|87")


def test_worker_remove_mesma_entrada_de_current_da_fila_sem_remover_repeticao_legitima() -> None:
    state = GuildMusicState(guild_id=1)
    current = AgentTrack(title="Mesmo Som", queue_item_id="entrada-a")
    alias = AgentTrack(title="Mesmo Som", queue_item_id="entrada-a")
    repeated = AgentTrack(title="Mesmo Som", queue_item_id="entrada-b")
    state.current = current
    state.queue = [alias, repeated]

    payload = state.public()

    assert payload["current"]["queue_item_id"] == "entrada-a"
    assert [item["queue_item_id"] for item in payload["queue"]] == ["entrada-b"]
    assert payload["queue_size"] == 1
    assert payload["queue_invariant_repairs"] == 1


def test_vps_tambem_filtra_snapshot_antigo_com_current_duplicado_na_queue() -> None:
    state = MusicGuildState()
    sincronizar_fila_remota(
        state,
        {
            "current": {"title": "Atual", "queue_item_id": "same"},
            "queue": [
                {"title": "Atual", "queue_item_id": "same"},
                {"title": "Atual", "queue_item_id": "legit-repeat"},
            ],
            "queue_size": 2,
            "virtual_playlist": _virtual_remote(total=87, next_offset=25, materialized_before=2),
        },
        limite_fila=100,
        limite_historico=25,
    )

    assert state.agent_remote_queue_size == 1
    assert [item.queue_item_id for item in state.forward_queue] == ["legit-repeat"]
    # O mesmo reparo precisa atingir a matemática do cursor, senão a duplicata
    # some visualmente mas continua somando +1 no total lógico.
    assert state.agent_virtual_playlist["materialized_before"] == 1
    assert logical_virtual_queue_count(
        total_tracks=state.agent_virtual_playlist["total_tracks"],
        next_offset=state.agent_virtual_playlist["next_offset"],
        materialized_before=state.agent_virtual_playlist["materialized_before"],
        remote_queue_size=state.agent_remote_queue_size,
    ) == 63


def test_assinatura_painel_distingue_repeticao_legitima_e_novo_playback_token() -> None:
    base = {
        "status": "playing",
        "confirmed_playing": True,
        "queue": [],
        "queue_size": 0,
        "playback_token": 10,
        "current": {
            "queue_item_id": "entrada-a",
            "title": "Mesmo Som",
            "webpage_url": "https://example.invalid/music",
            "duration": 180,
        },
    }
    repeated = {
        **base,
        "playback_token": 11,
        "current": {**base["current"], "queue_item_id": "entrada-b"},
    }

    assert _assinatura_painel_remoto(base) != _assinatura_painel_remoto(repeated)
