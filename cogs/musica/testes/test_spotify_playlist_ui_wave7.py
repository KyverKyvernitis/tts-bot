from __future__ import annotations

import ast
from pathlib import Path

from cogs.musica.agente_telefone.monitor import _assinatura_painel_remoto
from cogs.musica.nucleo.estado import MusicGuildState
from cogs.musica.reproducao.sincronizacao import sincronizar_fila_remota


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "interface" / "componentes.py"


def _virtual(*, total_tracks=None, next_offset: int = 25, materialized_before: int = 4, waiting: bool = False):
    return {
        "cursor": {
            "provider": "spotify_public",
            "source_url": "https://open.spotify.com/playlist/abc1234567890123456789",
            "title": "Playlist Gigante",
            "resource_type": "playlist",
            "resource_id": "abc1234567890123456789",
            "next_offset": next_offset,
            "total_tracks": total_tracks,
            "exhausted": False,
        },
        "materialized_before": materialized_before,
        "waiting": waiting,
    }


def _function_source(name: str) -> str:
    source = COMPONENTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


def _class_method_source(class_name: str, method_name: str) -> str:
    source = COMPONENTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    method = next(item for item in cls.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name)
    return ast.get_source_segment(source, method) or ""


def test_sync_espelha_playlist_virtual_sem_materializar_colecao() -> None:
    state = MusicGuildState()
    remote = {
        "queue": [],
        "queue_size": 0,
        "virtual_playlist": _virtual(total_tracks=None, next_offset=25, materialized_before=0, waiting=True),
    }

    sincronizar_fila_remota(state, remote, limite_fila=100, limite_historico=25)

    assert state.agent_virtual_playlist == {
        "active": True,
        "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/abc1234567890123456789",
        "title": "Playlist Gigante",
        "resource_type": "playlist",
        "next_offset": 25,
        "total_tracks": None,
        "materialized_before": 0,
        "waiting": True,
    }
    assert list(state.forward_queue) == []


def test_sync_limpa_playlist_virtual_quando_marker_some() -> None:
    state = MusicGuildState()
    state.agent_virtual_playlist = {"active": True, "title": "antiga"}

    sincronizar_fila_remota(state, {"queue": [], "queue_size": 0}, limite_fila=100, limite_historico=25)

    assert state.agent_virtual_playlist == {}


def test_assinatura_do_painel_muda_quando_cursor_virtual_avanca() -> None:
    before = {
        "status": "playing",
        "queue": [],
        "queue_size": 0,
        "virtual_playlist": _virtual(next_offset=25),
    }
    after = {
        "status": "playing",
        "queue": [],
        "queue_size": 0,
        "virtual_playlist": _virtual(next_offset=50),
    }

    assert _assinatura_painel_remoto(before) != _assinatura_painel_remoto(after)


def test_preview_virtual_e_compacto_e_sem_detalhes_internos() -> None:
    block = _function_source("_queue_preview_text")
    total_label = _function_source("_virtual_playlist_total_label")

    assert 'return ""' in total_label
    assert "if virtual:" in block
    assert "carregando próximas…" in block
    assert "restante da playlist carregado automaticamente conforme necessário" not in block
    assert "duração carregada" not in block
    assert "já carregada" not in block


def test_marker_virtual_sem_faixa_materializada_nao_vira_idle() -> None:
    presentation = _function_source("_player_status_presentation")
    build = _class_method_source("MusicPlayerView", "_build")

    assert 'return "Carregando playlist", PLAYER_STATUS_ANIMATED_EMOJI' in presentation
    assert "elif virtual:" in build
    assert "Carregando próximas músicas" in build
    assert "_queue_preview_text(state, limit=4)" in build


def test_queueview_permanece_operavel_quando_so_existe_cursor_virtual() -> None:
    refresh = _class_method_source("QueueView", "_refresh_components")
    clear = _class_method_source("QueueView", "clear_queue")
    queue_text = _class_method_source("QueueView", "_queue_text")

    assert "if (items or virtual)" in refresh
    assert "if items or virtual:" in refresh
    assert "not _virtual_playlist_info(state)" in clear
    assert "Carregando próximas músicas…" in queue_text
    assert "Duração:" not in queue_text


def test_modal_de_adicionar_preserva_direct_play_virtual_start_first() -> None:
    block = _class_method_source("AddSongModal", "on_submit")

    assert "bounded_initial_window(" in block
    assert "virtual_active = bool(batch.is_playlist" in block
    assert "is_multi = bool(len(batch.tracks) > 1 or virtual_active)" in block
    assert "payload_cursor_playlist(" in block
    assert 'music_agent_command(\n                        "enqueue_many"' in block
    assert "schedule_playlist_refill_from_result(self.router, guild.id, result)" in block
    assert "Playlist iniciada" in block


def test_render_virtual_continua_sem_io_novo() -> None:
    build = _class_method_source("MusicPlayerView", "_build")

    assert "await " not in build
    for forbidden in ("music_agent_status(", "resolve_music_tracks_on_worker(", "yt_dlp", "aiohttp", "requests."):
        assert forbidden not in build
