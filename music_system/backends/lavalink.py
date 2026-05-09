from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
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

    async def _request_json_any(
        self,
        paths: list[str],
        *,
        fallback_only_on_not_found: bool = True,
    ) -> tuple[Any, int, str]:
        if not self.cfg.configured:
            raise RuntimeError("Lavalink não configurado: defina host, porta e senha no painel `_musicnode`.")
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
                message="Node Lavalink respondeu. Playback real só é liberado no servidor de teste e no modo Lavalink/Auto.",
                latency_ms=latency_ms,
                version=version,
                players=players,
                playing_players=playing_players,
                extra={
                    "node": self.cfg.node_name,
                    "host": self.cfg.safe_host_label,
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
                extra={"node": self.cfg.node_name, "host": self.cfg.safe_host_label, "wavelink_installed": self._wavelink_installed()},
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
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Lavalink está desativado.")
        if not self.cfg.configured:
            return BackendSearchResult(backend=self.name, ok=False, query=query, message="Lavalink não configurado.")

        lower_query = query.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
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

    async def close_wavelink_pool(self) -> None:
        """Fecha conexões Wavelink globais quando o node é reconfigurado."""
        if not self._wavelink_installed():
            return
        try:
            wavelink = self._import_wavelink()
            close = getattr(getattr(wavelink, "Pool", None), "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            logger.debug("[music/lavalink] falha ao fechar pool Wavelink", exc_info=True)


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

    async def ensure_wavelink_pool(self, bot, *, force_reconnect: bool = False):
        """Garante que o Pool do Wavelink está conectado ao node configurado."""
        if not self.cfg.enabled or self.cfg.mode == "off":
            raise RuntimeError("Lavalink está desativado para este servidor.")
        if not self.cfg.configured:
            raise RuntimeError("Lavalink não configurado: defina host, porta e senha no painel `_musicnode`.")
        wavelink = self._import_wavelink()
        pool = getattr(wavelink, "Pool", None)
        if pool is None:
            raise RuntimeError("Wavelink instalado não expõe Pool compatível.")

        stale_existing = False
        if not force_reconnect:
            with contextlib.suppress(Exception):
                existing = pool.get_node(self.cfg.node_name)
                if existing is not None:
                    if self._node_is_connected(existing):
                        return wavelink, existing
                    stale_existing = True
                    logger.info(
                        "[music/lavalink] node Wavelink existe, mas não está CONNECTED; reconectando pool | node=%s",
                        self.cfg.node_name,
                    )

        if force_reconnect or stale_existing:
            await self.close_wavelink_pool()

        node_cls = getattr(wavelink, "Node", None)
        if node_cls is None:
            raise RuntimeError("Wavelink instalado não expõe Node compatível.")
        try:
            node = node_cls(uri=self.cfg.base_url, password=self.cfg.password, identifier=self.cfg.node_name)
        except TypeError:
            node = node_cls(uri=self.cfg.base_url, password=self.cfg.password)
        kwargs = {"nodes": [node], "client": bot}
        # Wavelink 3 recomenda cache pequeno/experimental. Se a assinatura local não aceitar,
        # o fallback sem cache mantém compatibilidade.
        try:
            await pool.connect(**kwargs, cache_capacity=100)
        except TypeError:
            await pool.connect(**kwargs)
        # Dá um pequeno tempo para o Pool atualizar o estado interno antes do
        # primeiro Player.connect/play. Em nodes públicos isso evita uma corrida
        # onde REST responde OK, mas o Pool ainda não está CONNECTED.
        await asyncio.sleep(0.35)
        with contextlib.suppress(Exception):
            refreshed = pool.get_node(self.cfg.node_name)
            if refreshed is not None:
                node = refreshed
        if not self._node_is_connected(node):
            logger.warning(
                "[music/lavalink] Pool conectado, mas node ainda não está CONNECTED | node=%s",
                self.cfg.node_name,
            )
        return wavelink, node

    def _playable_candidates(self, track: Any, *, fallback_query: str = "") -> list[str]:
        candidates: list[str] = []
        for value in (
            getattr(track, "original_url", ""),
            getattr(track, "webpage_url", ""),
            fallback_query,
            getattr(track, "title", ""),
        ):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates

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
                    search = await wavelink.Playable.search(candidate)
                except Exception as pool_exc:
                    message = str(pool_exc).lower()
                    if (not reconnected_after_pool_error) and ("no nodes" in message or "connected state" in message or "pool" in message):
                        reconnected_after_pool_error = True
                        logger.info(
                            "[music/lavalink] Pool Wavelink sem node conectado durante busca; reconectando uma vez | query=%r",
                            candidate,
                        )
                        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=True)
                        search = await wavelink.Playable.search(candidate)
                    else:
                        raise
                playable = None
                tracks = getattr(search, "tracks", None)
                if tracks:
                    playable = list(tracks)[0]
                elif isinstance(search, list) and search:
                    playable = search[0]
                elif hasattr(search, "__iter__"):
                    found = list(search)
                    if found:
                        playable = found[0]
                if playable is None:
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

    async def connect_player(self, bot, guild: Any, channel: Any, *, force_reconnect: bool = False):
        wavelink, _node = await self.ensure_wavelink_pool(bot, force_reconnect=force_reconnect)
        player_cls = getattr(wavelink, "Player", None)
        player = getattr(guild, "voice_client", None)
        if player_cls is None:
            raise RuntimeError("Wavelink instalado não expõe Player compatível.")
        if player is not None and not isinstance(player, player_cls):
            # Em modo Lavalink real não misture dois donos de voz. Se houver áudio
            # local/TTS ativo, falha de forma controlada; se estiver ocioso, limpa o
            # voice client local antes do Wavelink assumir.
            local_active = False
            with contextlib.suppress(Exception):
                local_active = bool(player.is_playing() or player.is_paused())
            if local_active:
                raise RuntimeError("Voice client local/TTS ainda está ativo; aguarde ele terminar antes de usar Lavalink real.")
            with contextlib.suppress(Exception):
                await player.disconnect(force=False)
            player = None
        if player is None or not bool(getattr(player, "connected", False)):
            player = await channel.connect(cls=player_cls, self_deaf=True)
        elif getattr(getattr(player, "channel", None), "id", None) != getattr(channel, "id", None):
            await player.move_to(channel)
        with contextlib.suppress(Exception):
            await guild.change_voice_state(channel=channel, self_deaf=True)
        return player

    async def play_track(self, bot, guild: Any, channel: Any, track: Any, *, volume: float = 1.0) -> tuple[Any, Any, dict[str, Any]]:
        # Primeiro resolve a faixa. Isso força/valida o Pool antes de abrir a
        # conexão de voz, evitando cair para voice local quando o Pool ainda não
        # terminou de conectar.
        playable, meta = await self.find_playable(bot, track)
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
        target_volume = max(0, min(150, int(round(float(volume or 1.0) * 100))))
        with contextlib.suppress(Exception):
            await player.set_volume(target_volume)
        try:
            await player.play(playable, add_history=False)
        except TypeError:
            await player.play(playable)
        return player, playable, meta


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
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            try:
                search = await wavelink.Playable.search(candidate)
                playable = None
                tracks = getattr(search, "tracks", None)
                if tracks:
                    playable = list(tracks)[0]
                elif isinstance(search, list) and search:
                    playable = search[0]
                elif hasattr(search, "__iter__"):
                    found = list(search)
                    if found:
                        playable = found[0]
                if playable is None:
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

        previous_playable = self._player_current_track(player)
        previous_position = self._player_position_ms(player)
        was_paused = self._player_bool(player, "paused", "is_paused")
        music_volume = self._player_volume_percent(player, resume_volume)
        tts_volume = max(0, min(150, int(round(float(volume or 1.0) * 100))))
        play_call_started_at = time.monotonic()
        restored = False
        restore_error: Exception | None = None
        try:
            await self._play_with_compat(player, tts_playable, start=0, volume=tts_volume, add_history=False)
            play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)
            playback_started_at = time.monotonic()
            deadline = playback_started_at + max(1.0, float(timeout or 120.0))
            last_active = playback_started_at
            while time.monotonic() < deadline:
                current = self._player_current_track(player)
                if current is not None and not self._same_playable(current, tts_playable):
                    break
                active = self._player_bool(player, "playing", "is_playing")
                if active:
                    last_active = time.monotonic()
                elif time.monotonic() - last_active > 0.45:
                    break
                await asyncio.sleep(0.08)
            else:
                raise RuntimeError(f"Playback TTS via Lavalink excedeu {float(timeout or 120.0):.1f}s")
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": resolved_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
                "tts_lavalink": True,
                "tts_lavalink_query": meta.get("query", ""),
            }
        finally:
            allowed = True
            if callable(should_resume):
                with contextlib.suppress(Exception):
                    allowed = bool(should_resume())
            if previous_playable is not None and allowed:
                try:
                    await self._play_with_compat(
                        player,
                        previous_playable,
                        start=max(0, int(previous_position)),
                        volume=music_volume,
                        add_history=False,
                    )
                    if was_paused:
                        await self._pause_player(player, True)
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

