from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import config

from .base import BackendHealth, BackendSearchResult

logger = logging.getLogger(__name__)


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _normalize_mode(value: object) -> str:
    raw = str(value or "off").strip().lower()
    if raw in {"shadow", "test", "diagnostic", "diagnóstico", "diagnostico"}:
        return "shadow"
    if raw in {"real", "lavalink", "active", "ativo", "on"}:
        return "lavalink"
    if raw in {"auto", "fallback"}:
        return "auto"
    return "off"


@dataclass(slots=True)
class LavalinkConfig:
    enabled: bool
    mode: str
    host: str
    port: int
    password: str
    secure: bool
    node_name: str
    timeout_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port > 0 and self.password)

    @property
    def base_url(self) -> str:
        raw_host = (self.host or "").strip().rstrip("/")
        if not raw_host:
            return ""
        if raw_host.startswith("http://") or raw_host.startswith("https://"):
            return raw_host
        scheme = "https" if self.secure else "http"
        # Permite LAVALINK_HOST tanto como "host" quanto "host:porta".
        if ":" in raw_host.rsplit("/", 1)[-1]:
            return f"{scheme}://{raw_host}"
        return f"{scheme}://{raw_host}:{self.port}"

    @property
    def safe_host_label(self) -> str:
        raw_host = (self.host or "").strip()
        if not raw_host:
            return "não configurado"
        raw_host = raw_host.replace("https://", "").replace("http://", "")
        return raw_host.split("/")[0]


class LavalinkBackend:
    """Backend Lavalink em modo diagnóstico.

    Este patch não usa Lavalink para tocar música. Ele só valida configuração,
    saúde do node e carregamento de tracks via REST para preparar a migração
    sem risco para o player FFmpeg/yt-dlp atual.
    """

    name = "lavalink"

    def __init__(self, cfg: LavalinkConfig) -> None:
        self.cfg = cfg
        self._session = None
        self._session_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, cfg: LavalinkConfig | None = None) -> "LavalinkBackend":
        if cfg is None:
            cfg = LavalinkConfig(
                enabled=_as_bool(getattr(config, "LAVALINK_ENABLED", False), False),
                mode=_normalize_mode(getattr(config, "LAVALINK_MODE", "off")),
                host=str(getattr(config, "LAVALINK_HOST", "") or "").strip(),
                port=max(1, _safe_int(getattr(config, "LAVALINK_PORT", 2333), 2333)),
                password=str(getattr(config, "LAVALINK_PASSWORD", "") or "").strip(),
                secure=_as_bool(getattr(config, "LAVALINK_SECURE", False), False),
                node_name=str(getattr(config, "LAVALINK_NODE_NAME", "main") or "main").strip() or "main",
                timeout_seconds=max(2.0, float(getattr(config, "LAVALINK_TIMEOUT_SECONDS", 8.0) or 8.0)),
            )
        return cls(cfg)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Client-Name": "tts-bot-lavalink-diagnostics/1.0",
        }
        if self.cfg.password:
            headers["Authorization"] = self.cfg.password
        return headers

    async def _get_session(self):
        if self._session is not None and not getattr(self._session, "closed", True):
            return self._session
        async with self._session_lock:
            if self._session is not None and not getattr(self._session, "closed", True):
                return self._session
            try:
                import aiohttp
            except Exception as exc:  # pragma: no cover - depende do ambiente da VPS
                raise RuntimeError("aiohttp indisponível; instale dependências do bot antes de testar Lavalink.") from exc
            timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers())
            return self._session

    async def _request_json(self, path: str, *, fallback_path: str | None = None) -> tuple[Any, int]:
        if not self.cfg.configured:
            raise RuntimeError("Lavalink não configurado: defina host, porta e senha no painel `_musicnode`.")
        session = await self._get_session()
        paths = [path]
        if fallback_path:
            paths.append(fallback_path)
        last_status = 0
        last_text = ""
        for item in paths:
            url = f"{self.cfg.base_url}{item}"
            try:
                async with session.get(url) as resp:
                    last_status = int(getattr(resp, "status", 0) or 0)
                    if 200 <= last_status < 300:
                        ctype = str(resp.headers.get("Content-Type", "") or "").lower()
                        if "json" in ctype:
                            return await resp.json(), last_status
                        return await resp.text(), last_status
                    last_text = (await resp.text())[:240]
            except Exception:
                if item == paths[-1]:
                    raise
                continue
        raise RuntimeError(f"HTTP {last_status}: {last_text or 'sem resposta útil'}")

    def _wavelink_installed(self) -> bool:
        return importlib.util.find_spec("wavelink") is not None

    async def health(self) -> BackendHealth:
        if not self.cfg.enabled or self.cfg.mode == "off":
            return BackendHealth(
                name=self.name,
                enabled=False,
                configured=self.cfg.configured,
                available=False,
                mode=self.cfg.mode,
                message="Lavalink desativado. Player local continua como backend real.",
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "wavelink_installed": self._wavelink_installed()},
            )
        if not self.cfg.configured:
            return BackendHealth(
                name=self.name,
                enabled=True,
                configured=False,
                available=False,
                mode=self.cfg.mode,
                message="Lavalink ativado, mas faltam host, porta ou senha no painel `_musicnode`.",
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "wavelink_installed": self._wavelink_installed()},
            )

        start = time.perf_counter()
        try:
            version_raw, _ = await self._request_json("/version")
            version = str(version_raw or "").strip()
            stats = None
            with contextlib.suppress(Exception):
                stats, _ = await self._request_json("/v4/stats", fallback_path="/stats")
            latency_ms = int((time.perf_counter() - start) * 1000)
            players = None
            playing_players = None
            if isinstance(stats, dict):
                players = _safe_int(stats.get("players"), 0)
                playing_players = _safe_int(stats.get("playingPlayers"), 0)
            return BackendHealth(
                name=self.name,
                enabled=True,
                configured=True,
                available=True,
                mode=self.cfg.mode,
                message="Node Lavalink respondeu. Nenhuma música real foi roteada por ele neste patch.",
                latency_ms=latency_ms,
                version=version,
                players=players,
                playing_players=playing_players,
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "wavelink_installed": self._wavelink_installed()},
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.debug("[music/lavalink] health check falhou", exc_info=True)
            return BackendHealth(
                name=self.name,
                enabled=True,
                configured=True,
                available=False,
                mode=self.cfg.mode,
                message=str(exc) or exc.__class__.__name__,
                latency_ms=latency_ms,
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "wavelink_installed": self._wavelink_installed()},
            )

    def _track_info(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        info = raw.get("info")
        if isinstance(info, dict):
            return info
        return raw

    def _summarize_loadtracks(self, payload: Any, query: str, latency_ms: int) -> BackendSearchResult:
        if not isinstance(payload, dict):
            return BackendSearchResult(
                backend=self.name,
                ok=False,
                query=query,
                latency_ms=latency_ms,
                message="Resposta inesperada do Lavalink.",
            )

        load_type = str(payload.get("loadType") or payload.get("load_type") or "unknown")
        data = payload.get("data")
        tracks: list[Any] = []
        playlist_name = ""

        if isinstance(data, list):
            tracks = data
        elif isinstance(data, dict):
            if isinstance(data.get("tracks"), list):
                tracks = data.get("tracks") or []
                info = data.get("info") or data.get("playlistInfo") or {}
                if isinstance(info, dict):
                    playlist_name = str(info.get("name") or info.get("title") or "")
            elif load_type.lower() in {"track", "shortcut"} or "info" in data:
                tracks = [data]

        first_info = self._track_info(tracks[0]) if tracks else {}
        title = str(first_info.get("title") or first_info.get("name") or "")
        author = str(first_info.get("author") or first_info.get("artist") or "")
        source = str(first_info.get("sourceName") or first_info.get("source") or "")

        error_message = ""
        if load_type.lower() in {"error", "loadfailed"}:
            if isinstance(data, dict):
                error_message = str(data.get("message") or data.get("cause") or "")
            error_message = error_message or "Lavalink retornou erro ao carregar a busca."

        ok = bool(tracks) and load_type.lower() not in {"error", "loadfailed"}
        return BackendSearchResult(
            backend=self.name,
            ok=ok,
            query=query,
            load_type=load_type,
            tracks_found=len(tracks),
            playlist_name=playlist_name,
            first_title=title,
            first_author=author,
            first_source=source,
            latency_ms=latency_ms,
            message="OK" if ok else (error_message or "Nenhuma música encontrada pelo Lavalink."),
        )

    async def search(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        query = (query or "").strip()
        if not query:
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Busca vazia.")
        if not self.cfg.enabled or self.cfg.mode == "off":
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Lavalink está desativado.")
        if not self.cfg.configured:
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Lavalink não configurado.")

        identifier = query if "://" in query else f"ytsearch:{query}"
        start = time.perf_counter()
        try:
            payload, _ = await self._request_json(
                f"/v4/loadtracks?identifier={quote(identifier, safe='')}",
                fallback_path=f"/loadtracks?identifier={quote(identifier, safe='')}",
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            return self._summarize_loadtracks(payload, query, latency_ms)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.debug("[music/lavalink] teste de loadtracks falhou", exc_info=True)
            return BackendSearchResult(
                backend=self.name,
                ok=False,
                query=query,
                latency_ms=latency_ms,
                message=str(exc) or exc.__class__.__name__,
            )

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not getattr(session, "closed", True):
            await session.close()

