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
    return SimpleNamespace(
        forward_queue=deque(tracks[:4]),
        queue=SimpleNamespace(_queue=[]),
        agent_remote_queue_size=total,
        agent_queue_layout=[{"kind": "track", "track": track} for track in tracks],
        agent_virtual_playlists=([{"active": True, "remaining": remaining, "total_tracks": total}]
                                 if remaining is None or remaining > 0 else []),
    )


def test_fila_com_cinquenta_faixas_exibe_duracao_total_mesmo_com_preview_curto() -> None:
    state = _state([_track(index) for index in range(50)], total=50)
    panel = _queue_preview_text(state)
    assert "**Fila** · 50 músicas · 2:30:00" in panel
    assert "+ 46 músicas" in panel


def test_playlist_virtual_exibe_duracao_conhecida_como_parcial() -> None:
    state = _state([_track(index) for index in range(25)], total=50, remaining=25)
    panel = _queue_preview_text(state)
    assert "**Fila** · 50 músicas · ≥ 1:15:00" in panel
    assert "2:30:00" not in panel


def test_fila_sem_duracao_conhecida_indica_pendente() -> None:
    state = _state([_track(1, duration=None)], total=50, remaining=49)
    assert "**Fila** · 50 músicas · duração pendente" in _queue_preview_text(state)
