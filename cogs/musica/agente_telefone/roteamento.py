from __future__ import annotations

from dataclasses import dataclass
import asyncio
import ipaddress
import json
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from cogs.musica import configuracao as config

from .modelos import MusicWorkerSelection
from .utilitarios import _phone_worker_base_url


@dataclass(frozen=True, slots=True)
class DestinoWorker:
    worker_id: str
    name: str
    base: str
    token: str
    transport: str = "phone-worker"


_DESTINOS_GUILD: dict[int, DestinoWorker] = {}
_DIRECT_HEALTH_CACHE: tuple[tuple[str, str], float, MusicWorkerSelection] | None = None


class _NoDirectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Nunca encaminhe o Bearer privado a um destino escolhido por redirect.
        return None


urlopen = build_opener(_NoDirectRedirect()).open


def configured_direct_destination() -> DestinoWorker | None:
    """Destino opt-in para um Music Agent independente, sem trocar dono ativo."""
    if not bool(getattr(config, "MUSIC_AGENT_DIRECT_API_ENABLED", False)):
        return None
    base = str(getattr(config, "MUSIC_AGENT_DIRECT_API_BASE_URL", "") or "").strip().rstrip("/")
    token = str(getattr(config, "MUSIC_AGENT_DIRECT_API_TOKEN", "") or "").strip()
    parsed = urlsplit(base)
    if (not token or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("configure o endpoint privado e token do Music Agent direto")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("porta inválida do Music Agent direto") from exc
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        private = host == "localhost" or host.endswith(".ts.net")
        try:
            address = ipaddress.ip_address(host)
            private = private or address.is_loopback or (
                address.version == 4 and any(address in network for network in (
                    ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                    ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("100.64.0.0/10"),
                ))
            ) or (address.version == 6 and address in ipaddress.ip_network("fc00::/7"))
        except ValueError:
            pass
        if not private:
            raise ValueError("Music Agent direto exige HTTPS ou endereço privado/Tailscale")
    return DestinoWorker("music-agent-direct", "Music Agent VPS", base, token, "direct")


def control_destination(guild_id: int, action: str) -> DestinoWorker | None:
    # Arquivamento permanece no worker de metadados, inclusive com voz na VPS.
    if str(action or "").strip().lower().replace("-", "_").startswith("archive_"):
        return None
    return destino_vinculado(guild_id) or configured_direct_destination()


async def post_json_destino(
    *, destino: DestinoWorker, payload: Mapping[str, Any], timeout_seconds: float,
    max_erro: int = 400, post_json: Any = None,
) -> dict[str, Any]:
    if post_json is None:
        from .transporte_http import post_json_worker
        post_json = post_json_worker
    direct = destino.transport == "direct"
    body = dict(payload)
    if direct:
        body.pop("task", None)
    return await post_json(
        url=f"{destino.base}/{'command' if direct else 'task'}", token=destino.token,
        payload=body, timeout_seconds=timeout_seconds, max_erro=max_erro,
    )


def _direct_health_selection(destino: DestinoWorker) -> MusicWorkerSelection:
    global _DIRECT_HEALTH_CACHE
    now = time.monotonic()
    key = (destino.base, destino.token)
    if _DIRECT_HEALTH_CACHE and _DIRECT_HEALTH_CACHE[0] == key and now - _DIRECT_HEALTH_CACHE[1] < 0.8:
        return _DIRECT_HEALTH_CACHE[2]
    available = False
    reason = "music_agent_direct_indisponivel"
    try:
        request = Request(f"{destino.base}/health", headers={"Authorization": f"Bearer {destino.token}"})
        with urlopen(request, timeout=3.0) as response:
            # Health é pequeno. Nunca transfira áudio nem snapshots ilimitados.
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("health excedeu orçamento")
        data = json.loads(raw)
        version = tuple(int(part) for part in str(data.get("version") or "0").split(".")[:3])
        available = bool(data.get("ok", True) and data.get("discord_ready")
                         and data.get("executor_mode", "full") in {"voice", "full"}
                         and data.get("voice_dependencies", {}).get("ok")
                         and version >= (0, 3, 83))
        reason = "ok" if available else "music_agent_direct_nao_pronto"
    except Exception:
        # Não inclua token, URL assinada ou corpo de erro na seleção/logs.
        pass
    selection = MusicWorkerSelection(available, worker_id=destino.worker_id, name=destino.name,
                                    reason=reason, worker={"endpoint": destino.base, "transport": "direct"})
    _DIRECT_HEALTH_CACHE = (key, now, selection)
    return selection


async def direct_music_worker_selection() -> MusicWorkerSelection | None:
    destino = configured_direct_destination()
    return await asyncio.to_thread(_direct_health_selection, destino) if destino else None


def _normalizar_base(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def destino_da_selecao(selection: MusicWorkerSelection) -> DestinoWorker | None:
    worker = selection.worker if isinstance(selection.worker, Mapping) else {}
    base = _normalizar_base(worker.get("endpoint")) or _normalizar_base(_phone_worker_base_url())
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        return None
    return DestinoWorker(
        worker_id=str(selection.worker_id or worker.get("worker_id") or "").strip(),
        name=str(selection.name or worker.get("name") or selection.worker_id or "Phone Worker").strip(),
        base=base,
        token=token,
    )


def destino_vinculado(guild_id: int) -> DestinoWorker | None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return None
    return _DESTINOS_GUILD.get(guild) if guild > 0 else None


def resolver_destino_worker(
    selection: MusicWorkerSelection,
    *,
    guild_id: int = 0,
    preferir_vinculo: bool = True,
) -> DestinoWorker | None:
    if preferir_vinculo:
        bound = destino_vinculado(guild_id)
        if bound is not None:
            return bound
    return destino_da_selecao(selection)


def vincular_guild_worker(guild_id: int, destino: DestinoWorker) -> None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return
    if guild > 0:
        _DESTINOS_GUILD[guild] = destino


def desvincular_guild_worker(guild_id: int) -> None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return
    if guild > 0:
        _DESTINOS_GUILD.pop(guild, None)


def limpar_vinculos_worker() -> None:
    _DESTINOS_GUILD.clear()


def escopo_cache_worker(selection: MusicWorkerSelection) -> str:
    destino = destino_da_selecao(selection)
    if destino is None:
        return str(selection.worker_id or selection.name or "").strip().lower()
    return (destino.worker_id or destino.base).strip().lower()


__all__ = [
    "DestinoWorker",
    "configured_direct_destination",
    "control_destination",
    "post_json_destino",
    "direct_music_worker_selection",
    "destino_da_selecao",
    "destino_vinculado",
    "resolver_destino_worker",
    "vincular_guild_worker",
    "desvincular_guild_worker",
    "limpar_vinculos_worker",
    "escopo_cache_worker",
]
