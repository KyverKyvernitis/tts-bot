from __future__ import annotations

from dataclasses import dataclass

from cogs.musica import configuracao as config

from .modelos import MusicTrack, PlaylistCursor


@dataclass(frozen=True, slots=True)
class PlaylistWindowPolicy:
    """Política de backpressure para playlists virtuais.

    A fila materializada fica pequena mesmo quando a coleção lógica possui
    milhares de itens. A Wave seguinte usa ``low_watermark`` para decidir quando
    pedir a próxima janela e ``high_watermark`` como alvo após o refill.
    """

    low_watermark: int
    high_watermark: int

    @classmethod
    def from_config(cls) -> "PlaylistWindowPolicy":
        high = max(5, min(50, int(getattr(config, "MUSIC_PLAYLIST_WINDOW_SIZE", 25) or 25)))
        low = max(1, min(high - 1, int(getattr(config, "MUSIC_PLAYLIST_LOW_WATERMARK", 8) or 8)))
        return cls(low_watermark=low, high_watermark=high)

    def refill_limit(self, materialized_count: int) -> int:
        return max(0, self.high_watermark - max(0, int(materialized_count)))


def bounded_initial_window(
    tracks: list[MusicTrack],
    cursor: PlaylistCursor | None,
    *,
    policy: PlaylistWindowPolicy | None = None,
) -> tuple[list[MusicTrack], PlaylistCursor | None]:
    """Recorta somente a janela ativa e reposiciona o cursor corretamente.

    Esta função não busca nada e não copia metadata pesada além da lista curta
    retornada. Ela é a fronteira que impede ``enqueue_many`` de receber uma
    playlist inteira quando a ativação lazy ocorrer.
    """

    policy = policy or PlaylistWindowPolicy.from_config()
    selected = list(tracks[: policy.high_watermark])
    if cursor is None:
        return selected, None

    # O provider pode ter materializado um lote maior que a janela. O próximo
    # refill deve começar logo após os itens efetivamente enviados ao player.
    base_offset = max(0, int(cursor.next_offset) - len(tracks))
    next_cursor = PlaylistCursor(
        provider=cursor.provider,
        source_url=cursor.source_url,
        title=cursor.title,
        resource_type=cursor.resource_type,
        resource_id=cursor.resource_id,
        next_offset=base_offset + len(selected),
        total_tracks=cursor.total_tracks,
        exhausted=bool(cursor.exhausted and len(selected) >= len(tracks)),
    )
    return selected, next_cursor


def should_refill_playlist(
    materialized_count: int,
    cursor: PlaylistCursor | None,
    *,
    policy: PlaylistWindowPolicy | None = None,
) -> bool:
    if cursor is None or cursor.exhausted:
        return False
    policy = policy or PlaylistWindowPolicy.from_config()
    return max(0, int(materialized_count)) <= policy.low_watermark
