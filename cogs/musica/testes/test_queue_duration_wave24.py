from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from cogs.musica.interface.componentes import _queue_preview_text
from cogs.musica.nucleo.modelos import MusicTrack


def _track(index: int, *, duration: int | None = 180) -> MusicTrack:
    return MusicTrack(
        title=f"Faixa {index}", webpage_url=f"https://example.com/{index}",
        requester_id=1, duration=duration,
    )


def _state(tracks: list[MusicTrack], *, total: int, remaining: int | None = 0) -> SimpleNamespace:
    virtual = {
        "active": True, "remaining": remaining, "total_tracks": total,
        "next_offset": len(tracks), "provider": "spotify_public",
        "source_url": "https://open.spotify.com/playlist/example", "instance_id": "one",
    }
    return SimpleNamespace(
        forward_queue=deque(tracks[:4]),
        queue=SimpleNamespace(_queue=[]),
        agent_remote_queue_size=total,
        agent_queue_layout=[{"kind": "track", "track": track} for track in tracks]
                           + ([{"kind": "virtual", "virtual": virtual}] if remaining is None or remaining > 0 else []),
        agent_virtual_playlists=([virtual] if remaining is None or remaining > 0 else []),
        agent_virtual_duration_cache={},
        agent_virtual_duration_unknown=set(),
        agent_virtual_duration_ends={},
        agent_virtual_duration_failed_until={},
    )


def test_fila_com_cinquenta_faixas_exibe_duracao_total_mesmo_com_preview_curto() -> None:
    state = _state([_track(index) for index in range(50)], total=50)
    panel = _queue_preview_text(state)
    assert "**Fila** · 50 músicas · 2:30:00" in panel
    assert "+ 46 músicas" in panel


def test_playlist_virtual_aguarda_duracao_total() -> None:
    state = _state([_track(index) for index in range(25)], total=50, remaining=25)
    panel = _queue_preview_text(state)
    assert "**Fila** · 50 músicas · calculando duração…" in panel
    assert "2:30:00" not in panel
    state.agent_virtual_duration_cache[("one", "spotify_public", "https://open.spotify.com/playlist/example")] = {
        index: 180 for index in range(25, 50)
    }
    assert "**Fila** · 50 músicas · 2:30:00" in _queue_preview_text(state)


def test_fila_sem_duracao_conhecida_indica_pendente() -> None:
    state = _state([_track(1, duration=None)], total=50, remaining=49)
    assert "**Fila** · 50 músicas · calculando duração…" in _queue_preview_text(state)
