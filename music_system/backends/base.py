from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from music_system.models import ExtractedBatch, MusicTrack


@dataclass(slots=True)
class BackendHealth:
    """Resumo seguro do estado de um backend de música.

    Não inclui senha, URL completa com credenciais ou qualquer segredo.
    """

    name: str
    enabled: bool
    configured: bool
    available: bool
    mode: str = "off"
    message: str = ""
    latency_ms: int | None = None
    version: str = ""
    players: int | None = None
    playing_players: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BackendSearchResult:
    """Resultado de teste/diagnóstico de carregamento de músicas."""

    backend: str
    ok: bool
    query: str
    load_type: str = ""
    tracks_found: int = 0
    playlist_name: str = ""
    first_title: str = ""
    first_author: str = ""
    first_source: str = ""
    latency_ms: int | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class MusicBackendAdapter(Protocol):
    """Contrato mínimo para backends de música.

    Patch 1 usa isso apenas para diagnóstico. Reprodução real continua no
    AudioRouter atual/local até o backend Lavalink ser ativado em patch futuro.
    """

    name: str

    async def health(self) -> BackendHealth:
        ...

    async def search(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        ...

    async def close(self) -> None:
        ...


class LocalPlaybackBackend:
    """Adapter fino para o player local atual.

    Ele existe para a arquitetura nova enxergar o backend local, mas não muda
    nenhuma chamada de playback do AudioRouter neste patch.
    """

    name = "local"

    def __init__(self, extractor) -> None:
        self.extractor = extractor

    async def health(self) -> BackendHealth:
        return BackendHealth(
            name=self.name,
            enabled=True,
            configured=True,
            available=True,
            mode="active",
            message="Player local FFmpeg/yt-dlp atual preservado.",
        )

    async def search(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        try:
            batch: ExtractedBatch = await self.extractor.extract(
                query,
                requester_id=int(requester_id or 0),
                requester_name=requester_name or "",
            )
        except Exception as exc:
            return BackendSearchResult(
                backend=self.name,
                ok=False,
                query=query,
                message=str(exc) or exc.__class__.__name__,
            )

        first: MusicTrack | None = batch.tracks[0] if batch.tracks else None
        return BackendSearchResult(
            backend=self.name,
            ok=bool(batch.tracks),
            query=query,
            load_type="playlist" if batch.is_playlist else ("track" if batch.tracks else "empty"),
            tracks_found=len(batch.tracks),
            playlist_name=batch.playlist_title or "",
            first_title=getattr(first, "title", "") or "",
            first_author=getattr(first, "uploader", "") or "",
            first_source=getattr(first, "source", "") or getattr(first, "extractor", "") or "",
            message="OK" if batch.tracks else "Nenhuma música encontrada pelo backend local.",
        )

    async def close(self) -> None:
        return None
