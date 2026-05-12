from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import config

from .base import BackendHealth, BackendSearchResult
from ..models import ExtractedBatch, MusicTrack

logger = logging.getLogger(__name__)


def _mirror_prefixes_from_config() -> tuple[str, ...]:
    """Fontes LavaSrc usadas para espelhar YouTube/Spotify antes do fallback local.

    O padrão evita YouTube no Lavalink e também evita depender de ``spsearch``
    quando o LavaSrc/Spotify está instável. Quem quiser testar Spotify/Deezer no
    node pode definir MUSIC_LAVASRC_MIRROR_PREFIXES="dzsearch,scsearch" no .env, mas Deezer só deve ser ligado quando a master key/ARL existirem.
    """
    raw = str(getattr(config, "MUSIC_LAVASRC_MIRROR_PREFIXES", "") or os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "") or "").strip()
    if not raw:
        raw = "scsearch"
    allowed = {"scsearch", "spsearch", "dzsearch", "amsearch"}
    out: list[str] = []
    for item in re.split(r"[,;\s]+", raw):
        prefix = item.strip().lower().removesuffix(":")
        if prefix in allowed and prefix not in out:
            out.append(prefix)
    return tuple(out or ["scsearch"])


MUSIC_LAVASRC_MIRROR_PREFIXES = _mirror_prefixes_from_config()


def _normalize_provider(value: object) -> str:
    raw = str(value or "lavalink").strip().lower()
    if raw in {"node", "nodelink", "node-link"}:
        return "nodelink"
    if raw in {"auto", "prefer_nodelink", "prefer-nodelink"}:
        return "auto"
    return "lavalink"


def _provider_label(value: object) -> str:
    provider = _normalize_provider(value)
    if provider == "nodelink":
        return "NodeLink"
    if provider == "auto":
        return "Auto"
    return "Lavalink"


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


def _parse_lavalink_major_version(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
            continue
        break
    if not digits:
        return None
    with contextlib.suppress(Exception):
        return int("".join(digits))
    return None


def _normalize_response_text(value: object, *, limit: int = 220) -> str:
    text = str(value or "").strip()
    if not text:
        return "sem corpo"
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _format_http_attempt(path: str, status: int, body: str) -> str:
    body_label = _normalize_response_text(body)
    if status in {401, 403}:
        return (
            f"{path} -> HTTP {status}: autorização recusada pelo node. "
            "Confira senha, porta e Secure/SSL no painel `_musicnode`. "
            f"Resposta: {body_label}"
        )
    if status == 404:
        return f"{path} -> HTTP 404: endpoint não encontrado neste node. Resposta: {body_label}"
    if status == 400:
        return f"{path} -> HTTP 400: requisição inválida para o Lavalink. Resposta: {body_label}"
    if status in {500, 502, 503, 504}:
        return (
            f"{path} -> HTTP {status}: node Lavalink público/externo indisponível ou sobrecarregado. "
            "O bot deve cair para o player local; tente outro node se isso repetir. "
            f"Resposta: {body_label}"
        )
    return f"{path} -> HTTP {status}: {body_label}"


def _decode_lavalink_response(body: str, content_type: str) -> Any:
    text = str(body or "").strip()
    # Lavalink `/version` retorna texto puro, mas alguns nodes públicos enviam
    # Content-Type incorreto como JSON. Tentar JSON cegamente gera erros como
    # "Extra data: line 1 column 4" em versões tipo "4.2.2".
    if not text:
        return ""
    if "json" not in str(content_type or "").lower():
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _coerce_version_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("version", "semver", "lavalinkVersion"):
            value = payload.get(key)
            if value:
                return str(value).strip()
    return str(payload or "").strip()


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
    provider: str = "lavalink"

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

    @property
    def provider_label(self) -> str:
        return _provider_label(self.provider)


class LavalinkBackend:
    """Backend Lavalink real usado pelo player de música.

    Valida o node via REST, resolve faixas pelo Wavelink e mantém fallbacks
    controlados para fontes tocáveis, priorizando estabilidade no Discord.
    """

    name = "lavalink"
    _shared_pool_locks: dict[int, asyncio.Lock] = {}
    _node_failure_until: dict[str, float] = {}
    _node_failure_reason: dict[str, str] = {}
    _search_cache: dict[str, tuple[float, list[MusicTrack]]] = {}

    def __init__(self, cfg: LavalinkConfig) -> None:
        self.cfg = cfg
        self.provider = _normalize_provider(getattr(cfg, "provider", "lavalink"))
        self._session = None
        self._session_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

    def _shared_pool_lock(self) -> asyncio.Lock:
        """Retorna uma trava global do Pool para todas as instâncias no loop atual."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._pool_lock
        key = id(loop)
        lock = self.__class__._shared_pool_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.__class__._shared_pool_locks[key] = lock
        return lock

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
                provider="lavalink",
            )
        return cls(cfg)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Client-Name": f"tts-bot-{self.provider}-diagnostics/1.0",
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

    async def _request_json_any(
        self,
        paths: list[str],
        *,
        fallback_only_on_not_found: bool = True,
    ) -> tuple[Any, int, str]:
        if not self.cfg.configured:
            raise RuntimeError(f"{self.cfg.provider_label} não configurado: defina host, porta e senha no painel `_musicnode`/env.")
        session = await self._get_session()
        attempts: list[str] = []
        for index, item in enumerate(paths):
            url = f"{self.cfg.base_url}{item}"
            try:
                async with session.get(url) as resp:
                    status = int(getattr(resp, "status", 0) or 0)
                    ctype = str(resp.headers.get("Content-Type", "") or "")
                    body = await resp.text()
                    if 200 <= status < 300:
                        return _decode_lavalink_response(body, ctype), status, item
                    attempts.append(_format_http_attempt(item, status, body))
                    if fallback_only_on_not_found and index < len(paths) - 1 and status not in {404, 405}:
                        break
            except Exception as exc:
                attempts.append(f"{item} -> {exc.__class__.__name__}: {exc}")
                if index == len(paths) - 1:
                    raise
        detail = " | ".join(attempts) if attempts else "sem resposta útil"
        raise RuntimeError(detail[:520])

    async def _request_json(self, path: str, *, fallback_path: str | None = None) -> tuple[Any, int]:
        paths = [path]
        if fallback_path:
            paths.append(fallback_path)
        payload, status, _ = await self._request_json_any(paths)
        return payload, status

    async def _detect_api_version(self) -> tuple[str, int | None]:
        version_raw, _, _ = await self._request_json_any(["/version"], fallback_only_on_not_found=False)
        version = _coerce_version_text(version_raw)
        return version, _parse_lavalink_major_version(version)

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
                message=f"{self.cfg.provider_label} desativado. Player local continua como backend real.",
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "provider": self.provider, "wavelink_installed": self._wavelink_installed()},
            )
        if not self.cfg.configured:
            return BackendHealth(
                name=self.name,
                enabled=True,
                configured=False,
                available=False,
                mode=self.cfg.mode,
                message=f"{self.cfg.provider_label} ativado, mas faltam host, porta ou senha no painel `_musicnode`/env.",
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "provider": self.provider, "wavelink_installed": self._wavelink_installed()},
            )

        start = time.perf_counter()
        try:
            version, api_major = await self._detect_api_version()
            stats = None
            stats_endpoint = ""
            with contextlib.suppress(Exception):
                stat_paths = ["/v4/stats"] if api_major and api_major >= 4 else (["/stats"] if api_major and api_major < 4 else ["/v4/stats", "/stats"])
                stats, _, stats_endpoint = await self._request_json_any(stat_paths)
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
                message=f"Node {self.cfg.provider_label} respondeu. Playback real disponível para servidores em modo Lavalink/Auto.",
                latency_ms=latency_ms,
                version=version,
                players=players,
                playing_players=playing_players,
                extra={
                    "node": self.cfg.node_name,
                    "host": self.cfg.safe_host_label,
                    "provider": self.provider,
                    "wavelink_installed": self._wavelink_installed(),
                    "api_major": api_major,
                    "stats_endpoint": stats_endpoint,
                },
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
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "provider": self.provider, "wavelink_installed": self._wavelink_installed()},
            )

    def _track_info(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        info = raw.get("info")
        if isinstance(info, dict):
            return info
        return raw

    def _summarize_loadtracks(
        self,
        payload: Any,
        query: str,
        latency_ms: int,
        *,
        endpoint: str = "",
        version: str = "",
        api_major: int | None = None,
    ) -> BackendSearchResult:
        if not isinstance(payload, dict):
            return BackendSearchResult(
                backend=self.name,
                ok=False,
                query=query,
                latency_ms=latency_ms,
                message="Resposta inesperada do Lavalink.",
                extra={"endpoint": endpoint, "version": version, "api_major": api_major},
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
            extra={"endpoint": endpoint, "version": version, "api_major": api_major},
        )

    async def search(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        query = (query or "").strip()
        if not query:
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Busca vazia.")
        if not self.cfg.enabled or self.cfg.mode == "off":
            return BackendSearchResult(backend=self.name, ok=False, query=query, message=f"{self.cfg.provider_label} está desativado.")
        if not self.cfg.configured:
            return BackendSearchResult(backend=self.name, ok=False, query=query, message=f"{self.cfg.provider_label} não configurado.")

        lower_query = query.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        # Em modo NodeLink/Lavalink, busca textual deve consultar YouTube por
        # padrão. Links e prefixos explícitos continuam intactos.
        identifier = query if "://" in query or lower_query.startswith(known_prefixes) else f"ytsearch:{query}"
        start = time.perf_counter()
        try:
            version, api_major = await self._detect_api_version()
            encoded_identifier = quote(identifier, safe="")
            if api_major and api_major >= 4:
                paths = [f"/v4/loadtracks?identifier={encoded_identifier}"]
            elif api_major and api_major < 4:
                paths = [f"/loadtracks?identifier={encoded_identifier}"]
            else:
                paths = [f"/v4/loadtracks?identifier={encoded_identifier}", f"/loadtracks?identifier={encoded_identifier}"]
            payload, _, endpoint = await self._request_json_any(paths)
            latency_ms = int((time.perf_counter() - start) * 1000)
            return self._summarize_loadtracks(
                payload,
                query,
                latency_ms,
                endpoint=endpoint.split("?", 1)[0],
                version=version,
                api_major=api_major,
            )
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



    def _import_wavelink(self):
        try:
            return importlib.import_module("wavelink")
        except Exception as exc:  # pragma: no cover - depende da VPS
            raise RuntimeError("Wavelink não está instalado. Atualize o ambiente com `pip install -r requirements.txt`.") from exc

    def _pool_private_nodes(self, pool: Any) -> dict[str, Any] | None:
        """Acessa o mapa interno do Wavelink 3.x para ejetar nodes quebrados.

        Wavelink 3.5 mantém nodes fechados em ``Pool._Pool__nodes`` quando
        ``Pool.close()`` chama ``Node.close()`` sem ``eject=True``. Se o bot
        tenta reconectar depois disso, o Wavelink registra "already have a node
        with identifier" e o Pool fica sem node CONNECTED.
        """
        with contextlib.suppress(Exception):
            nodes = getattr(pool, "_Pool__nodes", None)
            if isinstance(nodes, dict):
                return nodes
        return None

    async def _close_wavelink_node(self, node: Any, *, pool: Any | None = None, eject: bool = True) -> None:
        if node is None:
            return
        identifier = str(getattr(node, "identifier", "") or "")
        close = getattr(node, "close", None)
        if callable(close):
            try:
                result = close(eject=eject)
            except TypeError:
                result = close()
            if asyncio.iscoroutine(result):
                with contextlib.suppress(Exception):
                    await result
        if eject and pool is not None and identifier:
            nodes = self._pool_private_nodes(pool)
            if nodes is not None:
                nodes.pop(identifier, None)

    async def close_wavelink_pool(self) -> None:
        """Fecha e remove nodes Wavelink globais quando o node é reconfigurado.

        Em Wavelink 3.x, ``Pool.close()`` fecha os nodes, mas não remove os
        identificadores do Pool. O patch precisa ejetar/limpar o mapa interno
        para evitar duplicidade de identifier e estado preso em DISCONNECTED.
        """
        if not self._wavelink_installed():
            return
        try:
            wavelink = self._import_wavelink()
            pool = getattr(wavelink, "Pool", None)
            if pool is None:
                return
            nodes: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                raw_nodes = getattr(pool, "nodes", {})
                if isinstance(raw_nodes, dict):
                    nodes = dict(raw_nodes)
            if nodes:
                for node in nodes.values():
                    await self._close_wavelink_node(node, pool=pool, eject=True)
            else:
                close = getattr(pool, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        with contextlib.suppress(Exception):
                            await result
            private_nodes = self._pool_private_nodes(pool)
            if private_nodes is not None:
                private_nodes.clear()
            cache = getattr(pool, "cache", None)
            if callable(cache):
                with contextlib.suppress(Exception):
                    cache(None)
        except Exception:
            logger.debug("[music/lavalink] falha ao fechar/ejetar pool Wavelink", exc_info=True)


    def _node_is_connected(self, node: Any) -> bool:
        """Best-effort status check compatible with multiple Wavelink 3.x builds."""
        if node is None:
            return False
        for attr in ("status", "state"):
            value = getattr(node, attr, None)
            if value is None:
                continue
            name = str(getattr(value, "name", value) or "").upper()
            if "CONNECTED" in name and "DISCONNECTED" not in name:
                return True
            if name in {"CONNECTING", "DISCONNECTED", "DISCONNECTING"}:
                return False
        for attr in ("available", "connected", "is_connected"):
            value = getattr(node, attr, None)
            try:
                if callable(value):
                    value = value()
                if value is not None:
                    return bool(value)
            except Exception:
                continue
        # Se não há indicador público confiável, prefira reconectar. Em Wavelink 3
        # alguns nodes ficam no Pool mas não estão atribuídos/CONNECTED; reutilizar
        # cegamente gera "No nodes are currently assigned..." no primeiro play.
        return False

    def _node_matches_config(self, node: Any) -> bool:
        try:
            node_uri = str(getattr(node, "uri", "") or "").rstrip("/")
            cfg_uri = str(self.cfg.base_url or "").rstrip("/")
            if node_uri and cfg_uri and node_uri != cfg_uri:
                return False
        except Exception:
            pass
        try:
            password = str(getattr(node, "password", "") or "")
            if password and self.cfg.password and password != self.cfg.password:
                return False
        except Exception:
            pass
        return True

    async def _wait_for_connected_node(self, pool: Any, node: Any) -> Any:
        deadline = asyncio.get_running_loop().time() + max(3.0, min(15.0, float(self.cfg.timeout_seconds or 8.0)))
        current = node
        while True:
            with contextlib.suppress(Exception):
                refreshed = pool.get_node(self.cfg.node_name)
                if refreshed is not None:
                    current = refreshed
            if self._node_is_connected(current):
                return current
            if asyncio.get_running_loop().time() >= deadline:
                return current
            await asyncio.sleep(0.25)

    def _node_status_label(self, node: Any) -> str:
        if node is None:
            return "ausente"
        value = getattr(node, "status", None) or getattr(node, "state", None)
        return str(getattr(value, "name", value) or "desconhecido")

    def _player_node(self, player: Any) -> Any | None:
        for attr in ("node", "_node"):
            with contextlib.suppress(Exception):
                node = getattr(player, attr, None)
                if node is not None:
                    return node
        return None

    async def _player_session_id(self, player: Any) -> str:
        """Retorna a sessionId REST do node Wavelink atual."""
        deadline = asyncio.get_running_loop().time() + max(3.0, min(12.0, float(self.cfg.timeout_seconds or 8.0)))
        while True:
            node = self._player_node(player)
            for attr in ("session_id", "sessionId", "_session_id", "_sessionId"):
                with contextlib.suppress(Exception):
                    value = getattr(node, attr, None) if node is not None else None
                    if callable(value):
                        value = value()
                    if value:
                        return str(value)
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.20)
        raise RuntimeError("Wavelink conectou ao node, mas não expôs session_id do Lavalink.")

    async def _rest_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[Any, int]:
        if not self.cfg.configured:
            raise RuntimeError(f"{self.cfg.provider_label} não configurado: defina host, porta e senha no painel `_musicnode`/env.")
        ok_statuses = ok_statuses or {200}
        session = await self._get_session()
        url = f"{self.cfg.base_url}{path}"
        async with session.request(str(method or "GET").upper(), url, json=payload) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            ctype = str(resp.headers.get("Content-Type", "") or "")
            body = await resp.text()
            if status in ok_statuses:
                return _decode_lavalink_response(body, ctype), status
            raise RuntimeError(_format_http_attempt(path, status, body))

    async def _get_rest_player(self, player: Any, guild: Any) -> dict[str, Any] | None:
        session_id = await self._player_session_id(player)
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id <= 0:
            raise RuntimeError("Guild inválida para consultar player Lavalink.")
        try:
            payload, _ = await self._rest_json("GET", f"/v4/sessions/{session_id}/players/{guild_id}")
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def _lavalink_state_connected(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        state = payload.get("state") or {}
        if isinstance(state, dict):
            return bool(state.get("connected"))
        return False

    async def _patch_voice_channel_id(self, player: Any, guild: Any, channel: Any, payload: dict[str, Any]) -> bool:
        """Reenvia o voice update incluindo channelId para Lavalink 4.x novo.

        Wavelink 3.5 pode enviar apenas token/endpoint/sessionId. Em builds novas
        do Lavalink, channelId também é necessário para completar a conexão de voz;
        sem ele o REST mostra track carregada, paused=false, mas state.connected=false
        e a música nunca fica verde no Discord.
        """
        voice = payload.get("voice") if isinstance(payload, dict) else None
        if not isinstance(voice, dict):
            return False
        token = str(voice.get("token") or "").strip()
        endpoint = str(voice.get("endpoint") or "").strip()
        session_id_voice = str(voice.get("sessionId") or voice.get("session_id") or "").strip()
        channel_id = str(getattr(channel, "id", "") or "").strip()
        if not token or not endpoint or not session_id_voice or not channel_id:
            return False
        desired = dict(voice)
        desired["token"] = token
        desired["endpoint"] = endpoint
        desired["sessionId"] = session_id_voice
        desired["channelId"] = channel_id
        if str(voice.get("channelId") or "") == channel_id:
            return False
        session_id = await self._player_session_id(player)
        guild_id = int(getattr(guild, "id", 0) or 0)
        await self._rest_json(
            "PATCH",
            f"/v4/sessions/{session_id}/players/{guild_id}?noReplace=true",
            payload={"voice": desired},
        )
        logger.info(
            "[music/lavalink] voice update reenviado com channelId | guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return True

    async def _wait_for_rest_voice_connected(
        self,
        player: Any,
        guild: Any,
        channel: Any,
        *,
        timeout: float = 10.0,
        allow_channel_patch: bool = True,
    ) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + max(2.0, float(timeout or 10.0))
        last_payload: dict[str, Any] | None = None
        patched = False
        while True:
            payload = await self._get_rest_player(player, guild)
            if payload is not None:
                last_payload = payload
                if self._lavalink_state_connected(payload):
                    return payload
                if allow_channel_patch and not patched:
                    try:
                        patched = await self._patch_voice_channel_id(player, guild, channel, payload)
                        if patched:
                            await asyncio.sleep(0.75)
                            continue
                    except Exception:
                        logger.debug("[music/lavalink] falha ao reenviar voice update com channelId", exc_info=True)
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.35)

        state = (last_payload or {}).get("state") if isinstance(last_payload, dict) else None
        voice = (last_payload or {}).get("voice") if isinstance(last_payload, dict) else None
        voice_keys = sorted([str(k) for k in voice.keys()]) if isinstance(voice, dict) else []
        raise RuntimeError(
            "Lavalink recebeu a música, mas não conectou à voz do Discord "
            f"(state={state!r}, voice_keys={voice_keys})."
        )

    def _node_failure_key(self) -> str:
        return "|".join([self.provider, self.cfg.node_name, self.cfg.base_url, str(bool(self.cfg.secure))])

    def failure_cooldown_remaining(self) -> float:
        until = float(self.__class__._node_failure_until.get(self._node_failure_key(), 0.0) or 0.0)
        return max(0.0, until - time.monotonic())

    def failure_cooldown_reason(self) -> str:
        return self.__class__._node_failure_reason.get(self._node_failure_key(), "")

    def _mark_node_failure(self, reason: object, *, seconds: float | None = None) -> None:
        cooldown = seconds
        if cooldown is None:
            cooldown = float(getattr(config, "AUDIO_NODE_FAILURE_COOLDOWN_SECONDS", 45.0) or 45.0)
        cooldown = max(5.0, float(cooldown or 45.0))
        key = self._node_failure_key()
        self.__class__._node_failure_until[key] = time.monotonic() + cooldown
        self.__class__._node_failure_reason[key] = _normalize_response_text(reason, limit=180)

    def _clear_node_failure(self) -> None:
        key = self._node_failure_key()
        self.__class__._node_failure_until.pop(key, None)
        self.__class__._node_failure_reason.pop(key, None)

    async def probe_ready(self) -> dict[str, Any]:
        """Healthcheck REST leve antes de entregar o Pool para Wavelink.

        Evita martelar websocket quando Java/NodeLink ainda está subindo.
        /v4/info é preferido porque confirma API v4; /version fica como fallback
        para nodes compatíveis que não expõem info completo.
        """
        if not self.cfg.configured:
            raise RuntimeError(f"{self.cfg.provider_label} não configurado: host/porta/senha ausentes.")
        start = time.perf_counter()
        payload, _status, endpoint = await self._request_json_any(["/v4/info", "/version"], fallback_only_on_not_found=True)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"endpoint": endpoint, "latency_ms": latency_ms, "payload": payload}

    async def ensure_wavelink_pool(self, bot, *, force_reconnect: bool = False):
        """Garante que o Pool do Wavelink está conectado ao node configurado.

        O Pool do Wavelink é global. Sem trava, dois comandos simultâneos podem
        tentar criar o mesmo ``identifier`` e deixar o Pool preso sem node
        CONNECTED. Também é necessário ejetar nodes antigos porque ``Pool.close``
        não remove o identificador em algumas versões 3.x.
        """
        if not self.cfg.enabled or self.cfg.mode == "off":
            raise RuntimeError(f"{self.cfg.provider_label} está desativado para este servidor.")
        if not self.cfg.configured:
            raise RuntimeError(f"{self.cfg.provider_label} não configurado: defina host, porta e senha no painel `_musicnode`/env.")

        remaining = self.failure_cooldown_remaining()
        if remaining > 0 and not force_reconnect:
            reason = self.failure_cooldown_reason() or "falha recente de conexão"
            raise RuntimeError(f"{self.cfg.provider_label} em cooldown por {remaining:.0f}s após {reason}. Usando fallback local quando permitido.")

        try:
            await self.probe_ready()
        except Exception as exc:
            self._mark_node_failure(exc)
            raise RuntimeError(f"{self.cfg.provider_label} ainda não está pronto no REST: {exc}") from exc

        async with self._shared_pool_lock():
            wavelink = self._import_wavelink()
            pool = getattr(wavelink, "Pool", None)
            if pool is None:
                raise RuntimeError("Wavelink instalado não expõe Pool compatível.")

            existing = None
            with contextlib.suppress(Exception):
                existing = pool.get_node(self.cfg.node_name)

            if existing is not None and not force_reconnect:
                if self._node_is_connected(existing) and self._node_matches_config(existing):
                    return wavelink, existing
                logger.info(
                    "[music/lavalink] node Wavelink existente será ejetado antes de reconectar | node=%s status=%s",
                    self.cfg.node_name,
                    self._node_status_label(existing),
                )

            if existing is not None or force_reconnect:
                # Fecha todos os nodes do Pool para evitar seleção do "best" node
                # em estado velho e para limpar cache de busca ligado ao node antigo.
                await self.close_wavelink_pool()

            node_cls = getattr(wavelink, "Node", None)
            if node_cls is None:
                raise RuntimeError("Wavelink instalado não expõe Node compatível.")
            try:
                node = node_cls(uri=self.cfg.base_url, password=self.cfg.password, identifier=self.cfg.node_name)
            except TypeError:
                node = node_cls(uri=self.cfg.base_url, password=self.cfg.password)

            kwargs = {"nodes": [node], "client": bot}
            try:
                try:
                    await pool.connect(**kwargs, cache_capacity=100)
                except TypeError:
                    await pool.connect(**kwargs)
            except Exception as exc:
                self._mark_node_failure(exc)
                raise

            node = await self._wait_for_connected_node(pool, node)
            if not self._node_is_connected(node):
                status = self._node_status_label(node)
                logger.warning(
                    "[music/lavalink] node não ficou CONNECTED após conectar o Pool | node=%s status=%s uri=%s",
                    self.cfg.node_name,
                    status,
                    self.cfg.base_url,
                )
                await self._close_wavelink_node(node, pool=pool, eject=True)
                error = RuntimeError(
                    f"{self.cfg.provider_label} ainda não está pronto/conectado. "
                    f"Node `{self.cfg.node_name}` ficou em `{status}`; tente novamente em alguns segundos."
                )
                self._mark_node_failure(error)
                raise error

            self._clear_node_failure()
            return wavelink, node

    _LAVALINK_SEARCH_PREFIXES = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")

    def _strip_lavalink_search_prefixes(self, value: object) -> str:
        text = str(value or "").strip()
        # Evita candidatos quebrados como ytmsearch:scsearch:termo.  O Wavelink
        # 3.x já adiciona o prefixo quando usamos o parâmetro source; por isso
        # qualquer prefixo interno precisa virar apenas o corpo da busca.
        while text:
            lower = text.lower()
            for prefix in self._LAVALINK_SEARCH_PREFIXES:
                if lower.startswith(prefix):
                    text = text[len(prefix) :].strip()
                    break
            else:
                return text
        return text

    def _split_lavalink_search_prefix(self, value: object) -> tuple[str | None, str]:
        text = str(value or "").strip()
        lower = text.lower()
        for prefix in self._LAVALINK_SEARCH_PREFIXES:
            if lower.startswith(prefix):
                return prefix[:-1], self._strip_lavalink_search_prefixes(text)
        return None, text

    def _append_unique_candidate(self, candidates: list[str], value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        prefix, body = self._split_lavalink_search_prefix(text)
        if prefix:
            if not body:
                return
            text = f"{prefix}:{body}"
        if text not in candidates:
            candidates.append(text)

    def _is_url_like_value(self, value: object) -> bool:
        text = str(value or "").strip().lower()
        return "://" in text or text.startswith(("spotify:", "applemusic:", "deezer:"))

    def _is_youtube_value(self, value: object) -> bool:
        text = str(value or "").strip().lower()
        return "youtube.com" in text or "youtu.be" in text or text.startswith(("ytsearch:", "ytmsearch:"))

    def _is_spotify_value(self, value: object) -> bool:
        text = str(value or "").strip().lower()
        return "open.spotify.com" in text or text.startswith(("spotify:", "spsearch:"))

    def _search_candidate(self, prefix: str, value: object) -> str:
        body = self._strip_lavalink_search_prefixes(value)
        return f"{prefix}:{body}" if body else ""

    def _mirror_search_candidates(self, track: Any, *, fallback_query: str = "") -> list[str]:
        """Candidatos LavaSrc seguros para espelhar metadata sem usar YouTube.

        Não envia URL crua de Spotify/YouTube para o Lavalink. Em vez disso usa
        título/artista/duração já conhecidos e prefixos configuráveis, por padrão
        ``scsearch``. Se nada bater, o AudioRouter cai para o player local.
        """
        query = self._metadata_search_query(track, fallback_query=fallback_query)
        if not query:
            return []
        out: list[str] = []
        for prefix in MUSIC_LAVASRC_MIRROR_PREFIXES:
            self._append_unique_candidate(out, self._search_candidate(prefix, query))
        return out

    def _is_generic_link_title(self, value: object) -> bool:
        text = self._strip_lavalink_search_prefixes(value).strip().lower()
        if not text:
            return True
        generic = {
            "link",
            "youtube",
            "youtube link",
            "soundcloud",
            "soundcloud link",
            "spotify",
            "spotify link",
            "deezer",
            "deezer link",
            "applemusic",
            "apple music",
            "applemusic link",
            "apple music link",
            "lavalink",
        }
        return text in generic or text.endswith(" link")

    def _plain_match_text(self, value: object) -> str:
        text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return " ".join(text.split())

    def _spotify_fallback_query(self, track: Any, *, fallback_query: str = "") -> str:
        # Mantido por compatibilidade interna, mas não é mais usado como primeira
        # escolha: Spotify no LavaSrc é Mirror e pode falhar/403. O fluxo novo usa
        # _mirror_search_candidates(), que por padrão evita spsearch.
        query = self._metadata_search_query(track, fallback_query=fallback_query)
        return self._search_candidate("spsearch", query) if query else ""

    def _is_direct_youtube_track(self, track: Any, *, fallback_query: str = "") -> bool:
        original_url = str(getattr(track, "original_url", "") or "").strip()
        fallback_query = str(fallback_query or "").strip()
        return bool(self._is_youtube_value(original_url) or self._is_youtube_value(fallback_query))

    def _requires_strict_mirror_match(self, track: Any, *, candidate: str = "") -> bool:
        source = str(getattr(track, "source", "") or getattr(track, "extractor", "") or "").strip().lower()
        original_url = str(getattr(track, "original_url", "") or "")
        webpage_url = str(getattr(track, "webpage_url", "") or "")
        prefix, _body = self._split_lavalink_search_prefix(candidate)
        mirror_prefixes = {"scsearch", "spsearch", "dzsearch", "amsearch"}
        return bool(
            source in {"youtube", "yt", "ytsearch", "ytmsearch", "spotify", "deezer", "apple", "metadata"}
            or self._is_youtube_value(webpage_url)
            or self._is_youtube_value(original_url)
            or self._is_spotify_value(webpage_url)
            or self._is_spotify_value(original_url)
            or prefix in mirror_prefixes
        )

    def _duration_seconds_from_meta(self, value: Any) -> float:
        try:
            numeric = float(value or 0)
        except Exception:
            return 0.0
        if numeric >= 10000:
            numeric /= 1000.0
        return max(0.0, numeric)

    def _mirror_meta_matches_track(self, track: Any, meta: dict[str, Any], *, candidate: str = "") -> bool:
        """Evita tocar música aleatória quando um resultado do YouTube é espelhado.

        Para pesquisa do YouTube, o resultado selecionado serve como metadata. O
        LavaSrc/SoundCloud/Spotify só é aceito se título/artista e duração forem
        compatíveis. Se não bater, o router cai para o yt-dlp local.
        """
        if not self._requires_strict_mirror_match(track, candidate=candidate):
            return True

        wanted_title = self._plain_match_text(getattr(track, "title", ""))
        wanted_author = self._plain_match_text(getattr(track, "uploader", ""))
        got_title = self._plain_match_text(meta.get("title", ""))
        got_author = self._plain_match_text(meta.get("author", ""))
        combined_wanted = " ".join(part for part in (wanted_author, wanted_title) if part).strip()
        combined_got = " ".join(part for part in (got_author, got_title) if part).strip()
        if not wanted_title or not got_title:
            return False

        title_ratio = SequenceMatcher(None, wanted_title, got_title).ratio()
        combined_ratio = SequenceMatcher(None, combined_wanted or wanted_title, combined_got or got_title).ratio()
        word_score = 0.0
        wanted_words = {w for w in wanted_title.split() if len(w) >= 3}
        got_words = set(got_title.split())
        if wanted_words:
            word_score = len(wanted_words & got_words) / max(1, len(wanted_words))

        expected_duration = self._duration_seconds_from_meta(getattr(track, "duration", None))
        got_duration = self._duration_seconds_from_meta(meta.get("duration"))
        duration_ok = True
        if expected_duration and got_duration:
            tolerance = max(15.0, expected_duration * 0.12)
            duration_ok = abs(expected_duration - got_duration) <= tolerance

        text_ok = title_ratio >= 0.52 or combined_ratio >= 0.48 or word_score >= 0.55
        return bool(text_ok and duration_ok)

    def _strip_duplicate_author_from_title(self, title: object, author: object) -> str:
        """Remove artista repetido no começo do título antes do mirror LavaSrc.

        A Spotify API às vezes entrega título já formatado como
        ``Artista - Música`` enquanto ``uploader/author`` também é ``Artista``.
        Sem essa normalização o mirror vira algo como
        ``scsearch:Artista Artista - Música``, piorando a chance do SoundCloud
        retornar outra faixa.
        """
        raw_title = str(title or "").strip()
        raw_author = str(author or "").strip()
        if not raw_title or not raw_author:
            return raw_title

        plain_title = self._plain_match_text(raw_title)
        plain_author = self._plain_match_text(raw_author)
        if not plain_title or not plain_author or not plain_title.startswith(plain_author + " "):
            return raw_title

        # Caminho principal: preserva o texto original do nome da música depois
        # de separadores comuns usados por Spotify/YouTube/SoundCloud.
        pattern = re.compile(r"^" + re.escape(raw_author) + r"\s*(?:[-–—:|•]+\s*)+", re.IGNORECASE)
        cleaned = pattern.sub("", raw_title, count=1).strip()
        if cleaned and self._plain_match_text(cleaned) != plain_author:
            return cleaned

        # Fallback por palavras quando há só espaço entre artista e título.
        author_words = plain_author.split()
        title_words = raw_title.split()
        if len(title_words) > len(author_words):
            candidate = " ".join(title_words[len(author_words):]).strip(" -–—:|•\t")
            if candidate and self._plain_match_text(candidate) != plain_author:
                return candidate
        return raw_title

    def _metadata_search_query(self, track: Any, *, fallback_query: str = "") -> str:
        title = self._strip_lavalink_search_prefixes(getattr(track, "title", "") or "")
        author = self._strip_lavalink_search_prefixes(getattr(track, "uploader", "") or getattr(track, "author", "") or "")
        if self._is_generic_link_title(title):
            title = ""
        title = self._strip_duplicate_author_from_title(title, author)
        if author and title:
            query = f"{author} - {title}"
        else:
            query = " ".join(part for part in (author, title) if part).strip()
        if query:
            return query
        fallback = self._strip_lavalink_search_prefixes(fallback_query)
        if fallback and not self._is_url_like_value(fallback) and not self._is_generic_link_title(fallback):
            return fallback
        return ""

    def _soundcloud_fallback_query(self, track: Any, *, fallback_query: str = "") -> str:
        query = self._metadata_search_query(track, fallback_query=fallback_query)
        return self._search_candidate("scsearch", query) if query else ""

    def _youtube_music_fallback_query(self, track: Any, *, fallback_query: str = "") -> str:
        query = self._metadata_search_query(track, fallback_query=fallback_query)
        return self._search_candidate("ytmsearch", query) if query else ""

    def _playable_candidates(self, track: Any, *, fallback_query: str = "") -> list[str]:
        candidates: list[str] = []
        original_url = str(getattr(track, "original_url", "") or "").strip()
        webpage_url = str(getattr(track, "webpage_url", "") or "").strip()
        source = str(getattr(track, "source", "") or getattr(track, "extractor", "") or "").strip().lower()
        is_youtube_track = (
            source in {"youtube", "yt", "ytsearch", "ytmsearch"}
            or self._is_youtube_value(original_url)
            or self._is_youtube_value(webpage_url)
            or self._is_youtube_value(fallback_query)
        )
        is_spotify_track = (
            source in {"spotify", "spsearch"}
            or self._is_spotify_value(original_url)
            or self._is_spotify_value(webpage_url)
            or self._is_spotify_value(fallback_query)
        )
        is_soundcloud_track = (
            source in {"soundcloud", "scsearch"}
            or "soundcloud.com" in original_url.lower()
            or "soundcloud.com" in webpage_url.lower()
            or "soundcloud.com" in str(fallback_query or "").lower()
        )

        raw_values = [fallback_query, original_url, webpage_url]
        url_values = [value for value in (original_url, webpage_url, fallback_query) if self._is_url_like_value(value)]
        non_url_queries = [value for value in (fallback_query, original_url, webpage_url) if value and not self._is_url_like_value(value)]
        explicit_prefixed = []
        for value in raw_values:
            prefix, body = self._split_lavalink_search_prefix(value)
            if prefix and body:
                explicit_prefixed.append(f"{prefix}:{body}")
        has_explicit_youtube_url = any(self._is_youtube_value(value) for value in url_values)
        sc_fallback = self._soundcloud_fallback_query(track, fallback_query=fallback_query)
        mirror_fallbacks = self._mirror_search_candidates(track, fallback_query=fallback_query)
        lavalink_playable = getattr(track, "lavalink_playable", None)
        lavalink_resolved = bool(getattr(track, "lavalink_resolved", False) or lavalink_playable is not None)

        if is_youtube_track:
            # YouTube nunca entra cru no Lavalink. Link direto e resultado de
            # pesquisa tentam primeiro um mirror LavaSrc sem YouTube; se não bater
            # ou o node perder o stream, o AudioRouter cai para yt-dlp local.
            for value in mirror_fallbacks:
                self._append_unique_candidate(candidates, value)
            return candidates

        if is_spotify_track:
            # Spotify também não entra cru no node. A metadata vem da Spotify API
            # no bot; o LavaSrc recebe apenas busca por título/artista em fonte
            # direta configurada (por padrão scsearch). Se falhar, fallback local.
            for value in mirror_fallbacks:
                self._append_unique_candidate(candidates, value)
            if not candidates:
                for value in explicit_prefixed:
                    if not str(value).lower().startswith(("spsearch:", "ytsearch:", "ytmsearch:")):
                        self._append_unique_candidate(candidates, value)
            return candidates

        if lavalink_resolved or url_values:
            # Link direto e resultado escolhido do node são escolhas explícitas.
            # Primeiro tenta exatamente o link/playable retornado. Para SoundCloud,
            # alguns itens resolvem metadata mas quebram no stream real com 404;
            # nesses casos é seguro tentar mirrors por título/artista depois do link
            # direto, desde que _mirror_meta_matches_track aprove o candidato.
            for value in url_values:
                self._append_unique_candidate(candidates, value)
            if not candidates:
                for value in explicit_prefixed:
                    self._append_unique_candidate(candidates, value)
            if is_soundcloud_track:
                for value in mirror_fallbacks:
                    self._append_unique_candidate(candidates, value)
            return candidates

        if explicit_prefixed:
            # Prefixos explícitos do LavaSrc/SoundCloud/Spotify são respeitados.
            # Prefixos YouTube não entram aqui porque YouTube é sempre local.
            for value in explicit_prefixed:
                if not str(value).lower().startswith(("ytsearch:", "ytmsearch:")):
                    self._append_unique_candidate(candidates, value)
            return candidates

        for value in url_values:
            self._append_unique_candidate(candidates, value)
        for value in mirror_fallbacks:
            self._append_unique_candidate(candidates, value)
        if not candidates:
            for value in non_url_queries:
                self._append_unique_candidate(candidates, self._search_candidate(MUSIC_LAVASRC_MIRROR_PREFIXES[0], value))
        return candidates

    def _looks_like_playable(self, value: Any) -> bool:
        if value is None or isinstance(value, (str, bytes, bytearray, dict)):
            return False
        # Wavelink 3.x pode retornar um Playable direto para URLs HTTP/MP3,
        # enquanto buscas normais retornam Search/Playlist/list. O fluxo antigo
        # só aceitava contêineres iteráveis; com TTS curto via URL pública o
        # Lavalink fazia o probe HEAD 206, mas o bot descartava o Playable direto
        # e registrava falso "não encontrou fonte tocável" antes de tocar.
        for attr in ("encoded", "identifier", "uri"):
            if getattr(value, attr, None):
                return True
        if getattr(value, "title", None) and (getattr(value, "length", None) is not None or getattr(value, "duration", None) is not None):
            return True
        return False

    def _first_playable_from_search(self, search: Any) -> Any | None:
        if self._looks_like_playable(search):
            return search
        tracks = getattr(search, "tracks", None)
        if tracks:
            try:
                items = list(tracks)
            except Exception:
                items = []
            for item in items:
                if self._looks_like_playable(item):
                    return item
            return items[0] if items else None
        data = getattr(search, "data", None)
        if data:
            if self._looks_like_playable(data):
                return data
            if isinstance(data, (list, tuple)) and data:
                return data[0]
        if isinstance(search, (list, tuple)) and search:
            return search[0]
        if isinstance(search, dict):
            return None
        try:
            iterator = iter(search)
        except TypeError:
            return None
        except Exception:
            return None
        for item in iterator:
            return item
        return None

    def _playables_from_search(self, search: Any, *, limit: int = 10) -> list[Any]:
        """Extrai uma lista de Playables de resultados Wavelink/NodeLink.

        Wavelink 3.x pode retornar Playable, Search, Playlist, lista/tupla ou
        objetos iteráveis. Para seleção textual precisamos preservar vários
        resultados em vez de escolher automaticamente o primeiro.
        """
        max_items = max(1, int(limit or 10))
        items: list[Any] = []

        def add(value: Any) -> None:
            if len(items) >= max_items:
                return
            if self._looks_like_playable(value):
                items.append(value)

        if self._looks_like_playable(search):
            add(search)
            return items

        tracks = getattr(search, "tracks", None)
        if tracks:
            with contextlib.suppress(Exception):
                for item in list(tracks):
                    add(item)
                    if len(items) >= max_items:
                        return items

        data = getattr(search, "data", None)
        if data:
            if self._looks_like_playable(data):
                add(data)
            elif isinstance(data, (list, tuple)):
                for item in data:
                    add(item)
                    if len(items) >= max_items:
                        return items

        if isinstance(search, (list, tuple)):
            for item in search:
                add(item)
                if len(items) >= max_items:
                    return items

        if not isinstance(search, dict):
            try:
                iterator = iter(search)
            except Exception:
                iterator = None
            if iterator is not None:
                for item in iterator:
                    add(item)
                    if len(items) >= max_items:
                        return items

        return items

    def _source_from_playable(self, playable: Any, *, uri: str = "", identifier: str = "") -> str:
        raw = str(getattr(playable, "source", "") or getattr(playable, "source_name", "") or "").strip().lower()
        raw = raw.replace("tracksource.", "").replace("source.", "")
        joined = " ".join(part for part in (raw, uri.lower(), identifier.lower()) if part)
        if "youtube" in joined or "youtu.be" in joined:
            return "youtube"
        if "soundcloud" in joined:
            return "soundcloud"
        if "spotify" in joined:
            return "spotify"
        if "deezer" in joined:
            return "deezer"
        if "apple" in joined:
            return "apple"
        return raw or "lavalink"

    def _uri_from_playable(self, playable: Any, *, source: str = "", identifier: str = "") -> str:
        for attr in ("uri", "url", "webpage_url"):
            value = str(getattr(playable, attr, "") or "").strip()
            if value:
                return value
        if source == "youtube" and identifier:
            return f"https://www.youtube.com/watch?v={identifier}"
        return ""

    def _encoded_from_playable(self, playable: Any) -> str:
        for attr in ("encoded", "track", "_encoded"):
            with contextlib.suppress(Exception):
                value = getattr(playable, attr, None)
                if callable(value):
                    value = value()
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _track_from_playable(
        self,
        playable: Any,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        query: str = "",
    ) -> MusicTrack:
        identifier = str(getattr(playable, "identifier", "") or "").strip()
        provisional_uri = str(getattr(playable, "uri", "") or getattr(playable, "url", "") or "").strip()
        source = self._source_from_playable(playable, uri=provisional_uri, identifier=identifier)
        uri = self._uri_from_playable(playable, source=source, identifier=identifier)
        title = str(getattr(playable, "title", "") or getattr(playable, "name", "") or "Música sem título").strip()
        author = str(getattr(playable, "author", "") or getattr(playable, "uploader", "") or "").strip()
        artwork = str(getattr(playable, "artwork", "") or getattr(playable, "thumbnail", "") or "").strip()
        duration_ms = self._duration_ms_from_meta(playable, None)
        duration = (duration_ms / 1000.0) if duration_ms > 0 else None
        is_live = bool(getattr(playable, "is_stream", False) or getattr(playable, "isStream", False) or getattr(playable, "stream", False))
        original = str(query or "").strip() or uri or identifier
        encoded = self._encoded_from_playable(playable)
        return MusicTrack(
            title=title,
            webpage_url=uri,
            original_url=original,
            requester_id=int(requester_id or 0),
            requester_name=requester_name or "",
            duration=duration,
            uploader=author,
            thumbnail=artwork,
            source=source,
            extractor="lavalink",
            is_live=is_live,
            lavalink_playable=playable,
            lavalink_encoded=encoded,
            lavalink_query=original,
            lavalink_resolved=True,
        )

    def _clone_cached_track(self, track: MusicTrack, *, requester_id: int = 0, requester_name: str = "") -> MusicTrack:
        try:
            return replace(
                track,
                requester_id=int(requester_id or 0),
                requester_name=requester_name or "",
            )
        except Exception:
            clone = MusicTrack(
                title=track.title,
                webpage_url=track.webpage_url,
                requester_id=int(requester_id or 0),
                requester_name=requester_name or "",
                stream_url=track.stream_url,
                duration=track.duration,
                uploader=track.uploader,
                thumbnail=track.thumbnail,
                source=track.source,
                original_url=track.original_url,
                extractor=track.extractor,
                is_live=track.is_live,
            )
            clone.lavalink_playable = getattr(track, "lavalink_playable", None)
            clone.lavalink_encoded = str(getattr(track, "lavalink_encoded", "") or "")
            clone.lavalink_query = str(getattr(track, "lavalink_query", "") or "")
            clone.lavalink_resolved = bool(getattr(track, "lavalink_resolved", False))
            return clone

    def _search_cache_key(self, query: str, *, limit: int) -> str:
        return "|".join([self.cfg.base_url, self.cfg.node_name, str(max(1, int(limit or 5))), re.sub(r"\s+", " ", str(query or "").strip().lower())])

    def _get_cached_selection_tracks(self, key: str, *, requester_id: int = 0, requester_name: str = "") -> list[MusicTrack] | None:
        ttl = max(0, int(getattr(config, "MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS", 90) or 0))
        if ttl <= 0 or not key:
            return None
        item = self.__class__._search_cache.get(key)
        if not item:
            return None
        created, tracks = item
        if time.monotonic() - created > ttl:
            self.__class__._search_cache.pop(key, None)
            return None
        return [self._clone_cached_track(track, requester_id=requester_id, requester_name=requester_name) for track in tracks]

    def _put_cached_selection_tracks(self, key: str, tracks: list[MusicTrack]) -> None:
        ttl = max(0, int(getattr(config, "MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS", 90) or 0))
        if ttl <= 0 or not key or not tracks:
            return
        cache = self.__class__._search_cache
        if len(cache) >= 128:
            oldest = sorted(cache.items(), key=lambda item: item[1][0])[:16]
            for old_key, _ in oldest:
                cache.pop(old_key, None)
        cache[key] = (time.monotonic(), [self._clone_cached_track(track, requester_id=track.requester_id, requester_name=track.requester_name) for track in tracks])

    async def extract_tracks_for_selection(
        self,
        bot,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        limit: int = 5,
    ) -> ExtractedBatch:
        """Busca faixas no Lavalink/NodeLink para UI de seleção.

        Diferente do fluxo de playback, esta função não chama ``yt-dlp`` nem
        escolhe automaticamente o primeiro resultado. Ela transforma os
        Playables retornados pelo node em ``MusicTrack`` com URL/candidato direto
        para o usuário escolher.
        """
        raw = str(query or "").strip()
        if not raw:
            return ExtractedBatch(tracks=[], query=raw, is_playlist=False)
        lower = raw.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        identifier = raw if lower.startswith(known_prefixes) or "://" in raw else f"scsearch:{raw}"
        cache_key = self._search_cache_key(identifier, limit=max(1, int(limit or 5)))
        cached = self._get_cached_selection_tracks(cache_key, requester_id=requester_id, requester_name=requester_name)
        if cached is not None:
            return ExtractedBatch(tracks=cached[: max(1, int(limit or 5))], query=identifier, is_playlist=False)
        wavelink, _node = await self.ensure_wavelink_pool(bot)
        search = await self._search_playable_candidate(wavelink, identifier)
        playables = self._playables_from_search(search, limit=limit)
        tracks = [
            self._track_from_playable(item, requester_id=requester_id, requester_name=requester_name, query=identifier)
            for item in playables[: max(1, int(limit or 5))]
        ]
        self._put_cached_selection_tracks(cache_key, tracks)
        return ExtractedBatch(tracks=tracks, query=identifier, is_playlist=False)

    async def extract_direct_tracks(
        self,
        bot,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        limit: int = 25,
    ) -> ExtractedBatch:
        """Resolve link direto no próprio node, sem yt-dlp e sem busca por título.

        Links diretos devem tocar exatamente o item retornado pelo Lavalink/NodeLink.
        Se o node não conseguir resolver/tocar aquele link, o erro precisa aparecer;
        não é seguro trocar automaticamente para busca textual por nomes genéricos
        como "Soundcloud link" ou "YouTube".
        """
        raw = str(query or "").strip()
        if not raw:
            return ExtractedBatch(tracks=[], query=raw, is_playlist=False)
        wavelink, _node = await self.ensure_wavelink_pool(bot)
        search = await self._search_playable_candidate(wavelink, raw)
        playables = self._playables_from_search(search, limit=max(1, int(limit or 25)))
        tracks = [
            self._track_from_playable(item, requester_id=requester_id, requester_name=requester_name, query=raw)
            for item in playables[: max(1, int(limit or 25))]
        ]
        return ExtractedBatch(tracks=tracks, query=raw, is_playlist=len(tracks) > 1)

    def _playable_debug_shape(self, value: Any) -> str:
        try:
            cls = f"{type(value).__module__}.{type(value).__qualname__}"
        except Exception:
            cls = type(value).__name__
        attrs: list[str] = []
        for attr in ("load_type", "loadType", "tracks", "data", "encoded", "identifier", "uri", "title", "length", "duration"):
            try:
                if getattr(value, attr, None) is not None:
                    attrs.append(attr)
            except Exception:
                continue
        return f"{cls} attrs={','.join(attrs) or '-'}"

    def _track_source_candidates(self, wavelink: Any, prefix: str | None) -> list[Any]:
        if not prefix:
            return []
        names_by_prefix = {
            "scsearch": ("SoundCloud", "SOUNDCLOUD", "soundcloud"),
            "ytsearch": ("YouTube", "YOUTUBE", "youtube"),
            "ytmsearch": ("YouTubeMusic", "YOUTUBE_MUSIC", "youtube_music"),
            "spsearch": ("Spotify", "SPOTIFY", "spotify"),
            "amsearch": ("AppleMusic", "APPLE_MUSIC", "apple_music"),
            "dzsearch": ("Deezer", "DEEZER", "deezer"),
        }
        sources: list[Any] = []
        track_source = getattr(wavelink, "TrackSource", None)
        if track_source is not None:
            for name in names_by_prefix.get(prefix, ()):  # Wavelink versions differ in enum casing.
                with contextlib.suppress(Exception):
                    value = getattr(track_source, name)
                    if value not in sources:
                        sources.append(value)
        # Algumas builds aceitam string no parâmetro source. Fica como fallback
        # sem quebrar as builds que aceitam apenas enum.
        for value in names_by_prefix.get(prefix, ())[::-1]:
            lowered = value.lower()
            if lowered not in sources:
                sources.append(lowered)
        return sources

    async def _search_playable_candidate(self, wavelink: Any, candidate: str) -> Any:
        prefix, body = self._split_lavalink_search_prefix(candidate)
        if prefix:
            # Wavelink 3.5 aplica um default search próprio quando recebe uma
            # string prefixada em Playable.search(). Em alguns nodes isso vira
            # identificadores quebrados como ``ytmsearch:scsearch:...``. Para
            # mirrors LavaSrc não-YouTube, use o TrackSource primeiro; só use a
            # forma direta para buscas YouTube explícitas/legadas.
            source_first_prefixes = {"scsearch", "spsearch", "amsearch", "dzsearch"}
            if prefix in source_first_prefixes:
                errors: list[Exception] = []
                for source in self._track_source_candidates(wavelink, prefix):
                    try:
                        return await wavelink.Playable.search(body, source=source)
                    except TypeError as exc:
                        errors.append(exc)
                        continue
                    except Exception as exc:
                        errors.append(exc)
                        # Se o source existe mas a fonte falhou, esse é o erro
                        # verdadeiro do node. Não tente direct string, pois isso
                        # reintroduz ytmsearch:scsearch.
                        raise

                pool = getattr(wavelink, "Pool", None)
                fetch_tracks = getattr(pool, "fetch_tracks", None) if pool is not None else None
                if callable(fetch_tracks):
                    try:
                        return await fetch_tracks(f"{prefix}:{body}")
                    except Exception as exc:
                        errors.append(exc)
                        raise

                if errors:
                    raise errors[-1]
                raise RuntimeError(f"Wavelink não expôs TrackSource compatível para {prefix}.")

            # Prefixos YouTube/YouTube Music ficam como compatibilidade legada.
            # O fluxo normal do bot não deve usar YouTube pelo Lavalink.
            direct_error: Exception | None = None
            try:
                return await wavelink.Playable.search(f"{prefix}:{body}")
            except Exception as exc:
                direct_error = exc

            type_error: Exception | None = None
            for source in self._track_source_candidates(wavelink, prefix):
                try:
                    return await wavelink.Playable.search(body, source=source)
                except TypeError as exc:
                    type_error = exc
                    continue
                except Exception:
                    raise
            if type_error is not None:
                logger.debug(
                    "[music/lavalink] busca prefixada direta e fallback source falharam | query=%r",
                    candidate,
                    exc_info=(type(direct_error), direct_error, direct_error.__traceback__) if direct_error else None,
                )
            if direct_error is not None:
                raise direct_error
        return await wavelink.Playable.search(candidate)

    async def find_playable(self, bot, track: Any, *, fallback_query: str = "") -> tuple[Any, dict[str, Any]]:
        """Resolve um MusicTrack atual para um Playable do Wavelink.

        Tenta URL original, URL resolvida pelo extrator local e por fim título.
        Isso preserva Spotify/Deezer via plugin quando o node suporta, mas mantém
        fallback para YouTube quando o extrator local já encontrou um equivalente.
        """
        wavelink, _node = await self.ensure_wavelink_pool(bot)
        last_error: Exception | None = None
        reconnected_after_pool_error = False
        for candidate in self._playable_candidates(track, fallback_query=fallback_query):
            try:
                try:
                    search = await self._search_playable_candidate(wavelink, candidate)
                except Exception as pool_exc:
                    message = str(pool_exc).lower()
                    if (not reconnected_after_pool_error) and ("no nodes" in message or "connected state" in message or "pool" in message):
                        reconnected_after_pool_error = True
                        logger.info(
                            "[music/lavalink] Pool Wavelink sem node conectado durante busca; reconectando uma vez | query=%r",
                            candidate,
                        )
                        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=True)
                        search = await self._search_playable_candidate(wavelink, candidate)
                    else:
                        raise
                playable = self._first_playable_from_search(search)
                if playable is None:
                    logger.debug(
                        "[music/lavalink] busca sem playable extraível | query=%r shape=%s",
                        candidate,
                        self._playable_debug_shape(search),
                    )
                    continue
                meta = {
                    "query": candidate,
                    "title": str(getattr(playable, "title", "") or getattr(track, "title", "") or ""),
                    "author": str(getattr(playable, "author", "") or getattr(track, "uploader", "") or ""),
                    "source": str(getattr(playable, "source", "") or getattr(track, "source", "") or ""),
                    "duration": getattr(playable, "length", None) or getattr(playable, "duration", None),
                    "artwork": str(getattr(playable, "artwork", "") or getattr(track, "thumbnail", "") or ""),
                }
                return playable, meta
            except Exception as exc:
                last_error = exc
                logger.debug("[music/lavalink] falha ao resolver playable | query=%r", candidate, exc_info=True)
        if last_error is not None:
            raise RuntimeError(f"Lavalink não resolveu a música: {last_error.__class__.__name__}: {last_error}") from last_error
        raise RuntimeError("Lavalink não encontrou uma faixa tocável para esta música.")

    def _bot_voice_client_for_guild(self, bot: Any, guild: Any) -> Any | None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        for vc in list(getattr(bot, "voice_clients", []) or []):
            with contextlib.suppress(Exception):
                if int(getattr(getattr(vc, "guild", None), "id", 0) or 0) == guild_id:
                    return vc
        return getattr(guild, "voice_client", None)

    async def _release_local_voice_client_for_lavalink(self, player: Any, guild: Any) -> None:
        # TTS/local voice client não pode coexistir com Wavelink na mesma guild.
        # Se ele estiver preso desde o boot ou durante uma tentativa anterior, pare e
        # desconecte para o Lavalink assumir a única conexão de voz do bot.
        if player is None:
            return
        with contextlib.suppress(Exception):
            stopper = getattr(player, "stop", None)
            if callable(stopper):
                stopper()
        with contextlib.suppress(Exception):
            await guild.change_voice_state(channel=None)
        with contextlib.suppress(Exception):
            await player.disconnect(force=True)
        await asyncio.sleep(0.35)

    async def connect_player(self, bot, guild: Any, channel: Any, *, force_reconnect: bool = False):
        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=force_reconnect)
        player_cls = getattr(wavelink, "Player", None)
        player = self._bot_voice_client_for_guild(bot, guild)
        if player_cls is None:
            raise RuntimeError("Wavelink instalado não expõe Player compatível.")

        if player is not None and not isinstance(player, player_cls):
            await self._release_local_voice_client_for_lavalink(player, guild)
            player = self._bot_voice_client_for_guild(bot, guild)
            if player is not None and not isinstance(player, player_cls):
                player = None

        if player is None or not bool(getattr(player, "connected", False)):
            try:
                player = await channel.connect(cls=player_cls, self_deaf=True)
            except Exception as exc:
                # discord.py pode levantar "Already connected to a voice channel"
                # mesmo quando guild.voice_client estava None, por causa de voice
                # client local preso no cache do Client. Releia o cache e recupere.
                if "already connected" not in str(exc).lower():
                    raise
                current = self._bot_voice_client_for_guild(bot, guild)
                if current is not None and isinstance(current, player_cls):
                    player = current
                else:
                    await self._release_local_voice_client_for_lavalink(current, guild)
                    player = await channel.connect(cls=player_cls, self_deaf=True)

        if getattr(getattr(player, "channel", None), "id", None) != getattr(channel, "id", None):
            await player.move_to(channel)
        with contextlib.suppress(Exception):
            await guild.change_voice_state(channel=channel, self_deaf=True)
        return player

    async def _resolve_playable_candidate(
        self,
        bot,
        wavelink: Any,
        track: Any,
        candidate: str,
    ) -> tuple[Any | None, dict[str, Any] | None, Any]:
        search = await self._search_playable_candidate(wavelink, candidate)
        playable = self._first_playable_from_search(search)
        if playable is None:
            logger.debug(
                "[music/lavalink] busca sem playable extraível | query=%r shape=%s",
                candidate,
                self._playable_debug_shape(search),
            )
            return None, None, search
        meta = {
            "query": candidate,
            "title": str(getattr(playable, "title", "") or getattr(track, "title", "") or ""),
            "author": str(getattr(playable, "author", "") or getattr(track, "uploader", "") or ""),
            "source": str(getattr(playable, "source", "") or getattr(track, "source", "") or ""),
            "duration": getattr(playable, "length", None) or getattr(playable, "duration", None) or getattr(track, "duration", None),
            "artwork": str(getattr(playable, "artwork", "") or getattr(track, "thumbnail", "") or ""),
        }
        return playable, meta, search

    def _duration_ms_from_meta(self, playable: Any, meta: dict[str, Any] | None = None) -> int:
        for value in (
            (meta or {}).get("duration") if isinstance(meta, dict) else None,
            getattr(playable, "length", None),
            getattr(playable, "duration", None),
        ):
            try:
                if value is None:
                    continue
                numeric = float(value)
                if numeric <= 0:
                    continue
                # yt-dlp/MusicTrack usa segundos; Wavelink geralmente usa ms.
                if numeric < 10000:
                    numeric *= 1000.0
                return max(0, int(numeric))
            except Exception:
                continue
        return 0

    def _rest_payload_has_track(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        track = payload.get("track")
        if track is None:
            return False
        if isinstance(track, dict):
            if track.get("encoded") or track.get("info") or track.get("pluginInfo"):
                return True
            return bool(track)
        return True

    async def _wait_for_playback_stable(
        self,
        player: Any,
        guild: Any,
        playable: Any,
        meta: dict[str, Any] | None,
        *,
        timeout: float = 2.2,
    ) -> None:
        """Garante que o track não caiu imediatamente após o start.

        O Lavalink pode carregar metadata e disparar o play, mas algumas fontes
        quebram somente quando o stream real é aberto (ex.: SoundCloud 404). Sem
        essa confirmação, o painel vira "tocando" e o worker trata a faixa como
        concluída naturalmente em menos de 1 segundo.
        """
        duration_ms = self._duration_ms_from_meta(playable, meta)
        deadline = asyncio.get_running_loop().time() + max(0.8, float(timeout or 2.2))
        started_at = asyncio.get_running_loop().time()
        saw_track = False
        last_payload: dict[str, Any] | None = None
        last_error: Exception | None = None

        while True:
            payload: dict[str, Any] | None = None
            with contextlib.suppress(Exception):
                payload = await self._get_rest_player(player, guild)
            if payload is not None:
                last_payload = payload

            player_current = self._player_current_track(player)
            rest_has_track = self._rest_payload_has_track(payload)
            playing_or_paused = self._player_bool(player, "playing", "is_playing", "paused", "is_paused")
            has_track_now = bool(player_current is not None or rest_has_track or playing_or_paused)
            if has_track_now:
                saw_track = True
            else:
                elapsed = asyncio.get_running_loop().time() - started_at
                # Para faixas de música normais, sumir nos primeiros segundos é
                # quase sempre TrackException/stream quebrado, não fim natural.
                if saw_track or duration_ms >= 8000 or elapsed >= 0.8:
                    state = (last_payload or {}).get("state") if isinstance(last_payload, dict) else None
                    last_error = RuntimeError(
                        "Lavalink iniciou a faixa, mas o player perdeu o track logo em seguida "
                        f"(duration_ms={duration_ms}, state={state!r})."
                    )
                    break

            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.25)

        raise last_error or RuntimeError("Lavalink perdeu o track logo após iniciar o playback.")

    async def _stop_player_quietly(self, player: Any | None) -> None:
        if player is None:
            return
        with contextlib.suppress(Exception):
            stop = getattr(player, "stop", None)
            if callable(stop):
                result = stop()
                if asyncio.iscoroutine(result):
                    await result

    def _meta_from_playable(self, playable: Any, track: Any, *, query: str = "") -> dict[str, Any]:
        return {
            "query": str(query or getattr(track, "lavalink_query", "") or getattr(track, "original_url", "") or getattr(track, "webpage_url", "") or ""),
            "title": str(getattr(playable, "title", "") or getattr(track, "title", "") or ""),
            "author": str(getattr(playable, "author", "") or getattr(track, "uploader", "") or ""),
            "source": str(getattr(playable, "source", "") or getattr(track, "source", "") or ""),
            "duration": getattr(playable, "length", None) or getattr(playable, "duration", None) or getattr(track, "duration", None),
            "artwork": str(getattr(playable, "artwork", "") or getattr(track, "thumbnail", "") or ""),
        }

    async def _play_pre_resolved_playable(
        self,
        bot,
        guild: Any,
        channel: Any,
        track: Any,
        playable: Any,
        *,
        target_volume: int,
    ) -> tuple[Any, Any, dict[str, Any]]:
        meta = self._meta_from_playable(playable, track)
        player = await self.connect_player(bot, guild, channel)
        with contextlib.suppress(Exception):
            await player.set_volume(target_volume)
        try:
            await self._play_with_compat(player, playable, volume=target_volume, add_history=False)
            await self._wait_for_rest_voice_connected(
                player,
                guild,
                channel,
                timeout=max(12.0, min(90.0, float(self.cfg.timeout_seconds or 12.0))),
            )
            await self._wait_for_playback_stable(player, guild, playable, meta, timeout=2.2)
        except Exception:
            await self._stop_player_quietly(player)
            raise
        return player, playable, meta

    async def play_track(self, bot, guild: Any, channel: Any, track: Any, *, volume: float = 1.0) -> tuple[Any, Any, dict[str, Any]]:
        # Resolve e toca cada candidato separadamente. Isso permite fallback real
        # quando uma fonte carrega metadata, mas quebra só no stream/playback.
        candidates = self._playable_candidates(track)
        if not candidates:
            raise RuntimeError("Lavalink não encontrou uma fonte candidata para esta música.")

        wavelink, _node = await self.ensure_wavelink_pool(bot)
        reconnected_after_pool_error = False
        last_error: Exception | None = None
        player: Any | None = None
        target_volume = max(0, min(150, int(round(float(volume or 1.0) * 100))))

        pre_resolved = getattr(track, "lavalink_playable", None)
        if self._looks_like_playable(pre_resolved):
            try:
                return await self._play_pre_resolved_playable(
                    bot,
                    guild,
                    channel,
                    track,
                    pre_resolved,
                    target_volume=target_volume,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[music/lavalink] playable já resolvido pelo node falhou | guild=%s track=%r erro=%s",
                    getattr(guild, "id", None),
                    getattr(track, "title", ""),
                    exc,
                )
                # Continua para os candidatos calculados. Para SoundCloud, isso pode
                # incluir mirrors por título/artista quando o stream direto cair com 404.

        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            playable = None
            meta: dict[str, Any] | None = None
            try:
                try:
                    playable, meta, _search = await self._resolve_playable_candidate(bot, wavelink, track, candidate)
                except Exception as pool_exc:
                    message = str(pool_exc).lower()
                    if (not reconnected_after_pool_error) and ("no nodes" in message or "connected state" in message or "pool" in message):
                        reconnected_after_pool_error = True
                        logger.info(
                            "[music/lavalink] Pool Wavelink sem node conectado durante busca; reconectando uma vez | query=%r",
                            candidate,
                        )
                        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=True)
                        playable, meta, _search = await self._resolve_playable_candidate(bot, wavelink, track, candidate)
                    else:
                        raise

                if playable is None or meta is None:
                    continue

                if not self._mirror_meta_matches_track(track, meta, candidate=candidate):
                    logger.info(
                        "[music/lavalink] mirror rejeitado por baixa compatibilidade | guild=%s query=%r wanted=%r got=%r",
                        getattr(guild, "id", None),
                        candidate,
                        getattr(track, "title", ""),
                        meta.get("title"),
                    )
                    last_error = RuntimeError("Espelho LavaSrc não bateu bem com a música escolhida.")
                    continue

                try:
                    player = await self.connect_player(bot, guild, channel)
                except Exception as exc:
                    message = str(exc).lower()
                    if "no nodes" in message or "connected state" in message or "pool" in message:
                        logger.info("[music/lavalink] Pool sem node no connect_player; reconectando uma vez | guild=%s", getattr(guild, "id", None))
                        await self.ensure_wavelink_pool(bot, force_reconnect=True)
                        player = await self.connect_player(bot, guild, channel, force_reconnect=False)
                    else:
                        raise

                with contextlib.suppress(Exception):
                    await player.set_volume(target_volume)
                try:
                    await self._play_with_compat(player, playable, volume=target_volume, add_history=False)
                    # Não aceite falso positivo: Lavalink pode receber track/voice e
                    # mesmo assim ficar state.connected=false. Nesse caso o painel dizia
                    # "tocando", mas nenhum áudio saía no Discord. Aguarde a conexão REST
                    # real e reenvie channelId quando o Wavelink antigo não enviar.
                    await self._wait_for_rest_voice_connected(
                        player,
                        guild,
                        channel,
                        timeout=max(12.0, min(90.0, float(self.cfg.timeout_seconds or 12.0))),
                    )
                    # Confirma também que o stream não caiu logo após abrir. Isso pega
                    # SoundCloud 404/TrackException e libera tentativa no próximo candidato.
                    await self._wait_for_playback_stable(player, guild, playable, meta, timeout=2.2)
                except Exception:
                    await self._stop_player_quietly(player)
                    raise

                if str(meta.get("query") or "") != candidate:
                    meta["query"] = candidate
                return player, playable, meta

            except Exception as exc:
                last_error = exc
                await self._stop_player_quietly(player)
                logger.warning(
                    "[music/lavalink] candidato falhou; tentando próximo fallback | guild=%s query=%r track=%r erro=%s",
                    getattr(guild, "id", None),
                    candidate,
                    getattr(track, "title", ""),
                    exc,
                )
                continue

        if last_error is not None:
            raise RuntimeError(f"Lavalink não conseguiu tocar nenhuma fonte candidata: {last_error.__class__.__name__}: {last_error}") from last_error
        raise RuntimeError("Lavalink não encontrou uma faixa tocável para esta música.")

    def _is_wavelink_player(self, player: Any) -> bool:
        if player is None:
            return False
        try:
            wavelink = self._import_wavelink()
            player_cls = getattr(wavelink, "Player", None)
            if player_cls is not None and isinstance(player, player_cls):
                return True
        except Exception:
            pass
        module = str(getattr(type(player), "__module__", "") or "")
        qualname = str(getattr(type(player), "__qualname__", "") or getattr(type(player), "__name__", "") or "")
        return module.startswith("wavelink") or (qualname == "Player" and hasattr(player, "node") and hasattr(player, "play"))

    def _player_current_track(self, player: Any) -> Any:
        for attr in ("current", "track", "playing"):
            value = getattr(player, attr, None)
            if value is not None and not isinstance(value, bool):
                return value
        return None

    def _player_position_ms(self, player: Any) -> int:
        for attr in ("position", "last_position"):
            value = getattr(player, attr, None)
            try:
                if callable(value):
                    value = value()
                if value is not None:
                    return max(0, int(float(value)))
            except Exception:
                continue
        state = getattr(player, "state", None)
        if isinstance(state, dict):
            with contextlib.suppress(Exception):
                return max(0, int(float(state.get("position") or 0)))
        return 0

    def _player_volume_percent(self, player: Any, fallback: float) -> int:
        value = getattr(player, "volume", None)
        try:
            if callable(value):
                value = value()
            if value is not None:
                return max(0, min(150, int(float(value))))
        except Exception:
            pass
        return max(0, min(150, int(round(float(fallback or 1.0) * 100))))

    def _same_playable(self, a: Any, b: Any) -> bool:
        if a is None or b is None:
            return False
        if a is b:
            return True
        for attr in ("encoded", "identifier", "uri", "title"):
            av = str(getattr(a, attr, "") or "")
            bv = str(getattr(b, attr, "") or "")
            if av and bv and av == bv:
                return True
        return False

    def _player_bool(self, player: Any, *attrs: str) -> bool:
        for attr in attrs:
            value = getattr(player, attr, None)
            try:
                if callable(value):
                    value = value()
                if bool(value):
                    return True
            except Exception:
                continue
        return False

    async def _pause_player(self, player: Any, paused: bool) -> None:
        if paused:
            pause = getattr(player, "pause", None)
            if callable(pause):
                try:
                    result = pause(True)
                except TypeError:
                    result = pause()
                if asyncio.iscoroutine(result):
                    await result
            return
        resume = getattr(player, "resume", None)
        if callable(resume):
            result = resume()
            if asyncio.iscoroutine(result):
                await result
            return
        pause = getattr(player, "pause", None)
        if callable(pause):
            try:
                result = pause(False)
            except TypeError:
                return
            if asyncio.iscoroutine(result):
                await result

    async def _set_player_volume(self, player: Any, volume: int) -> None:
        setter = getattr(player, "set_volume", None)
        if not callable(setter):
            return
        result = setter(max(0, min(150, int(volume))))
        if asyncio.iscoroutine(result):
            await result

    async def _ramp_player_volume(self, player: Any, start: int, end: int, duration_ms: int) -> None:
        """Rampa curta de volume para evitar click/flicker em troca de stream."""
        if not bool(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", True)):
            await self._set_player_volume(player, end)
            return
        duration_ms = max(0, int(duration_ms or 0))
        start = max(0, min(150, int(start)))
        end = max(0, min(150, int(end)))
        if duration_ms <= 0 or start == end:
            await self._set_player_volume(player, end)
            return
        steps = max(2, min(8, int(duration_ms / 35) or 2))
        delay = max(0.0, duration_ms / 1000.0 / steps)
        for idx in range(1, steps + 1):
            value = round(start + ((end - start) * idx / steps))
            with contextlib.suppress(Exception):
                await self._set_player_volume(player, int(value))
            if idx < steps and delay > 0:
                await asyncio.sleep(delay)

    async def _play_with_compat(
        self,
        player: Any,
        playable: Any,
        *,
        start: int | None = None,
        volume: int | None = None,
        add_history: bool | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"replace": True}
        if start is not None:
            kwargs["start"] = max(0, int(start))
        if volume is not None:
            kwargs["volume"] = max(0, min(150, int(volume)))
        if add_history is not None:
            kwargs["add_history"] = bool(add_history)
        try:
            return await player.play(playable, **kwargs)
        except TypeError:
            kwargs.pop("add_history", None)
            try:
                return await player.play(playable, **kwargs)
            except TypeError:
                kwargs.pop("volume", None)
                try:
                    result = await player.play(playable, **kwargs)
                except TypeError:
                    result = await player.play(playable)
                if volume is not None:
                    with contextlib.suppress(Exception):
                        await player.set_volume(max(0, min(150, int(volume))))
                return result

    async def find_first_playable_from_candidates(self, bot, candidates: list[str]) -> tuple[Any, dict[str, Any]]:
        wavelink, _node = await self.ensure_wavelink_pool(bot)
        last_error: Exception | None = None
        reconnected_after_pool_error = False
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            try:
                try:
                    search = await self._search_playable_candidate(wavelink, candidate)
                except Exception as pool_exc:
                    message = str(pool_exc).lower()
                    if (not reconnected_after_pool_error) and ("no nodes" in message or "connected state" in message or "pool" in message):
                        reconnected_after_pool_error = True
                        logger.info(
                            "[music/lavalink] Pool Wavelink sem node conectado durante TTS; reconectando uma vez | query=%r",
                            candidate,
                        )
                        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=True)
                        search = await self._search_playable_candidate(wavelink, candidate)
                    else:
                        raise
                playable = self._first_playable_from_search(search)
                if playable is None:
                    logger.debug(
                        "[music/lavalink] busca sem playable extraível | query=%r shape=%s",
                        candidate,
                        self._playable_debug_shape(search),
                    )
                    continue
                meta = {
                    "query": candidate,
                    "title": str(getattr(playable, "title", "") or "TTS"),
                    "author": str(getattr(playable, "author", "") or "TTS"),
                    "source": str(getattr(playable, "source", "") or "http"),
                    "duration": getattr(playable, "length", None) or getattr(playable, "duration", None),
                    "uri": str(getattr(playable, "uri", "") or candidate),
                }
                return playable, meta
            except Exception as exc:
                last_error = exc
                logger.debug("[music/lavalink] falha ao resolver TTS no Lavalink | query=%r", candidate, exc_info=True)
        if last_error is not None:
            raise RuntimeError(f"Lavalink não resolveu o TTS: {last_error.__class__.__name__}: {last_error}") from last_error
        raise RuntimeError("Lavalink não encontrou fonte tocável para o TTS.")

    async def play_tts_interrupt(
        self,
        bot,
        guild: Any,
        *,
        candidates: list[str],
        volume: float = 1.0,
        resume_volume: float = 1.0,
        resume_playable: Any | None = None,
        timeout: float = 120.0,
        should_resume=None,
    ) -> dict[str, Any]:
        """Toca um TTS curto pelo próprio Lavalink e restaura a música atual.

        O playable do TTS é resolvido antes de substituir a faixa atual. Assim, se
        o node não conseguir acessar o arquivo/URL, a música não é pausada nem
        substituída.
        """
        started_at = time.monotonic()
        tts_playable, meta = await self.find_first_playable_from_candidates(bot, candidates)
        resolved_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)

        player = getattr(guild, "voice_client", None)
        if player is None or not self._is_wavelink_player(player):
            raise RuntimeError("Player Lavalink não está conectado para tocar TTS.")

        await self._wait_for_rest_voice_connected(
            player,
            guild,
            getattr(player, "channel", None) or getattr(guild, "voice_client", None),
            timeout=6.0,
        )
        previous_playable = self._player_current_track(player) or resume_playable
        previous_position = self._player_position_ms(player)
        was_paused = self._player_bool(player, "paused", "is_paused")
        music_volume = self._player_volume_percent(player, resume_volume)
        tts_volume = max(0, min(150, int(round(float(volume or 1.0) * 100))))
        pause_before_tts = bool(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_ENABLED", True)) and previous_playable is not None and not was_paused
        pause_grace = max(0.05, float(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS", 0.35) or 0.35))
        ramp_ms = max(0, int(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", 180) or 0))
        ramp_floor = max(0, min(100, int(getattr(config, "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", 5) or 0)))
        ramp_floor_volume = max(0, min(music_volume, int(round(music_volume * (ramp_floor / 100.0)))))
        if pause_before_tts:
            try:
                if bool(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", True)) and music_volume > ramp_floor_volume and ramp_ms > 0:
                    await self._ramp_player_volume(player, music_volume, ramp_floor_volume, ramp_ms)
                await self._pause_player(player, True)
                # Releia a posição logo depois do pause para restaurar mais perto
                # do ponto real onde o TTS interrompeu a música.
                previous_position = max(previous_position, self._player_position_ms(player))
                logger.info(
                    "[music/lavalink] tts_pause_lavalink_start | guild=%s position_ms=%s volume=%s ramp_floor=%s ramp_ms=%s",
                    getattr(guild, "id", None),
                    previous_position,
                    music_volume,
                    ramp_floor_volume,
                    ramp_ms,
                )
                await asyncio.sleep(pause_grace)
            except Exception:
                logger.debug("[music/lavalink] falha ao pausar música antes do TTS; seguindo com interrupt", exc_info=True)
        play_call_started_at = time.monotonic()
        restored = False
        restore_error: Exception | None = None
        try:
            await self._play_with_compat(player, tts_playable, start=0, volume=tts_volume, add_history=False)
            # O player pode permanecer pausado porque pausamos a música anterior
            # antes de substituir a faixa. Sem este resume explícito, o payload REST
            # fica com ``paused: true`` e o TTS HTTP carrega, mas nunca toca.
            with contextlib.suppress(Exception):
                await self._pause_player(player, False)
            play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)
            playback_started_at = time.monotonic()
            deadline = playback_started_at + max(1.0, float(timeout or 120.0))
            last_active = playback_started_at
            resume_attempted_at = 0.0
            while time.monotonic() < deadline:
                current = self._player_current_track(player)
                if current is not None and not self._same_playable(current, tts_playable):
                    break
                active = self._player_bool(player, "playing", "is_playing")
                paused = self._player_bool(player, "paused", "is_paused")
                rest_active = False
                rest_paused = False
                with contextlib.suppress(Exception):
                    rest_payload = await self._get_rest_player(player, guild)
                    rest_state = (rest_payload or {}).get("state") or {}
                    rest_position = int(float(rest_state.get("position") or 0)) if isinstance(rest_state, dict) else 0
                    rest_connected = bool(rest_state.get("connected")) if isinstance(rest_state, dict) else False
                    rest_paused = bool(rest_state.get("paused")) if isinstance(rest_state, dict) else False
                    rest_active = bool(rest_connected and (rest_position > 0 or time.monotonic() - playback_started_at < 1.0))
                if paused or rest_paused:
                    now = time.monotonic()
                    if now - resume_attempted_at > 0.75:
                        resume_attempted_at = now
                        with contextlib.suppress(Exception):
                            await self._pause_player(player, False)
                        logger.info(
                            "[music/lavalink] tts_lavalink_forced_resume | guild=%s",
                            getattr(guild, "id", None),
                        )
                if active or rest_active:
                    last_active = time.monotonic()
                elif time.monotonic() - last_active > 0.85:
                    break
                await asyncio.sleep(0.10)
            else:
                logger.warning(
                    "[music/lavalink] tts_session_timeout | guild=%s timeout=%.1fs; restaurando música anterior",
                    getattr(guild, "id", None),
                    float(timeout or 120.0),
                )
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": resolved_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
                "tts_lavalink": True,
                "tts_lavalink_query": meta.get("query", ""),
                "tts_lavalink_source": meta.get("source", ""),
                "tts_lavalink_uri": meta.get("uri", ""),
                "tts_lavalink_timeout_recovered": playback_ms >= (max(1.0, float(timeout or 120.0)) * 1000.0),
            }
        finally:
            allowed = True
            if callable(should_resume):
                with contextlib.suppress(Exception):
                    allowed = bool(should_resume())
            if previous_playable is not None and allowed:
                try:
                    seek_ahead_ms = max(0, int(getattr(config, "MUSIC_TTS_RESUME_SEEK_AHEAD_MS", 120) or 0))
                    resume_position = max(0, int(previous_position) + seek_ahead_ms)
                    ramp_ms = max(0, int(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", 180) or 0))
                    ramp_floor = max(0, min(100, int(getattr(config, "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", 5) or 0)))
                    start_volume = music_volume
                    if bool(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", True)) and not was_paused and ramp_ms > 0:
                        start_volume = max(0, min(music_volume, int(round(music_volume * (ramp_floor / 100.0)))))
                    await self._play_with_compat(
                        player,
                        previous_playable,
                        start=resume_position,
                        volume=start_volume,
                        add_history=False,
                    )
                    await self._wait_for_rest_voice_connected(
                        player,
                        guild,
                        getattr(player, "channel", None) or getattr(guild, "voice_client", None),
                        timeout=6.0,
                    )
                    if was_paused:
                        await self._pause_player(player, True)
                    elif start_volume != music_volume:
                        await self._ramp_player_volume(player, start_volume, music_volume, ramp_ms)
                    logger.info(
                        "[music/lavalink] tts_pause_lavalink_resume | guild=%s restored=%s position_ms=%s seek_ahead_ms=%s volume_start=%s volume_final=%s ramp_ms=%s",
                        getattr(guild, "id", None),
                        True,
                        resume_position,
                        seek_ahead_ms,
                        start_volume,
                        music_volume,
                        ramp_ms,
                    )
                    restored = True
                except Exception as exc:
                    restore_error = exc
                    logger.warning("[music/lavalink] falha ao restaurar música após TTS", exc_info=True)
            if restore_error is not None:
                raise RuntimeError(f"TTS Lavalink terminou, mas a restauração da música falhou: {restore_error}") from restore_error
            if previous_playable is not None and not restored and allowed:
                logger.warning("[music/lavalink] TTS Lavalink terminou sem restaurar faixa anterior")

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not getattr(session, "closed", True):
            await session.close()

