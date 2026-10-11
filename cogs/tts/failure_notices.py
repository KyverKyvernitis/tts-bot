"""Avisos de TTS com destino fixado, sem reproduzir texto ou erros privados."""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
import sys
import time
from types import ModuleType, TracebackType
from urllib.error import HTTPError as UrllibHTTPError

import aiohttp
import discord
import requests

log = logging.getLogger(__name__)

# Todos os textos públicos são constantes; nunca incluir traceback, URLs ou
# conteúdo da fala no chat. Preservar a redação aprovada dos seis avisos base.
_MESSAGES = {
    "author_not_in_voice": "C tem que estar em call pra poder usar isso <:vish:1533479762230775898>",
    "voice_channel_mismatch": "C tem que estar na mesma call q eu <:speed:1534287805424074772>",
    "tts_disabled": "Texto pra fala tá desativado nesse sv chefe <:vish:1533479762230775898>",
    "tts_guild_disabled": "Texto pra fala tá desativado nesse sv chefe <:vish:1533479762230775898>",
    "service_unavailable": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "synthesis_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "tls_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "network_timeout": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "network_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "dns_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "voice_auth_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "voice_service_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "voice_rate_limited": "Tão limitando minhas falas agr, tenta dnv daqui a pouco <:speed:1534287805424074772>",
    "queue_unavailable": "Minha fila de fala tá com problema agr, tenta dnv <:speed:1534287805424074772>",
    "ignored_role": "C tem um cargo q não pode usar texto pra fala nesse sv <:vish:1533479762230775898>",
    "bot_muted": "Tô mutada nessa call 💔 (ps: alguém precisa tirar meu mute)",
    "bot_suppressed": "Me botaram na plateia, n consigo falar 💔",
    "voice_full": "Essa call tá cheia, n consigo entrar <:vish:1533479762230775898>",
    "voice_permission_missing": "Não consigo falar nessa call 💔",
    "teto_disabled": "Essa voz tá desativada agr <:vish:1533479762230775898>",
    "teto_configuration_invalid": "Essa voz n tá funcionando agora <:speed:1534287805424074772>",
    "opus_missing": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "ffmpeg_missing": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "no_frames": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "route_failed": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "no_audio_received": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "storage_denied": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "internal_error": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "connection_failed": "Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
    "audio_missing": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "audio_source_failed": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "playback_failed": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
    "tts_failed": "N consegui falar isso por algum motivo <:speed:1534287805424074772>",
}

# Os avisos da mesma classe se repetem no máximo uma vez a cada 30s por
# usuário/servidor, mas um problema diferente não fica escondido.
_NOTICE_CATEGORY = {
    **dict.fromkeys(("tts_disabled", "tts_guild_disabled"), "disabled"),
    **dict.fromkeys(("synthesis_failed", "tls_failed", "network_timeout", "network_failed", "dns_failed", "voice_auth_failed", "voice_service_failed", "service_unavailable", "internal_error"), "service"),
    **dict.fromkeys(("opus_missing", "ffmpeg_missing", "no_frames", "route_failed", "no_audio_received", "storage_denied", "audio_missing", "audio_source_failed", "playback_failed", "tts_failed"), "playback"),
    **dict.fromkeys(("voice_permission_missing", "connection_failed"), "voice_connection"),
}

_PERMISSION_LABELS = (("view_channel", "Ver canal"), ("connect", "Conectar"), ("speak", "Falar"))


def diagnose_voice_access(guild, voice_channel) -> tuple[str, str] | None:
    """Diagnóstico conservador: somente permissão/lotação confirmada."""
    me = getattr(guild, "me", None)
    if me is None or voice_channel is None:
        return None
    permissions_for = getattr(voice_channel, "permissions_for", None)
    if not callable(permissions_for):
        return None
    try:
        permissions = permissions_for(me)
        for attr, label in _PERMISSION_LABELS:
            if getattr(permissions, attr, None) is False:
                return "voice_permission_missing", label
        limit = getattr(voice_channel, "user_limit", None)
        members = getattr(voice_channel, "members", None)
        if type(limit) is int and limit > 0 and isinstance(members, (list, tuple)):
            already_present = getattr(getattr(getattr(me, "voice", None), "channel", None), "id", None) == getattr(voice_channel, "id", None)
            if not already_present and len(members) >= limit and getattr(permissions, "move_members", None) is False:
                return "voice_full", ""
    except Exception:
        # Permissões não disponíveis não provam bloqueio; não inventar motivo.
        return None
    return None


def _voice_channel_for_item(guild, item):
    channel_id = int(getattr(item, "channel_id", 0) or 0)
    if not channel_id:
        return None
    getter = getattr(guild, "get_channel", None)
    return getter(channel_id) if callable(getter) else None


def _public_notice(code: str, *, permission: str = "") -> str:
    if code == "voice_permission_missing" and permission:
        # Somente rótulos constantes, nunca strings vindas do Discord/usuário.
        allowed_labels = {label for _, label in _PERMISSION_LABELS}
        if permission in allowed_labels:
            return f"Não consigo falar nessa call 💔  (ps: se vc me der permissão de *{permission}* talvez eu consiga)"
    return _MESSAGES[code]



def _exception_types(module, *names: str) -> tuple[type[BaseException], ...]:
    # Algumas suítes usam módulos substitutos; nunca entregar um objeto que não
    # seja uma classe de exceção ao isinstance.
    return tuple(
        kind for name in names
        if isinstance(kind := getattr(module, name, None), type) and issubclass(kind, BaseException)
    )


_TLS_ERRORS = (ssl.SSLError,) + _exception_types(
    requests.exceptions, "SSLError",
) + _exception_types(aiohttp, "ClientSSLError", "ClientConnectorCertificateError")
_TIMEOUT_ERRORS = (TimeoutError,) + _exception_types(requests.exceptions, "Timeout")
_DNS_ERRORS = (socket.gaierror,) + _exception_types(aiohttp, "ClientConnectorDNSError")
_CONNECTION_ERRORS = (ConnectionError,) + _exception_types(
    requests.exceptions, "ConnectionError",
) + _exception_types(aiohttp, "ClientConnectionError")
_REQUEST_HTTP_ERRORS = _exception_types(requests.exceptions, "HTTPError")
_AIOHTTP_RESPONSE_ERRORS = _exception_types(aiohttp, "ClientResponseError")
_INTERNAL_ERRORS = (TypeError, AttributeError, NameError, KeyError, IndexError, AssertionError, NotImplementedError)
_INTERNAL_FRAME_FILES = {
    "cogs.tts.audio": "cogs/tts/audio.py",
    "cogs.tts.streaming": "cogs/tts/streaming.py",
    "cogs.tts.runtime": "cogs/tts/runtime.py",
    "cogs.tts.prepared": "cogs/tts/prepared.py",
    "cogs.tts.routing": "cogs/tts/routing.py",
    "cogs.tts.cog": "cogs/tts/cog.py",
}
_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:(?:a|b|rc)[0-9]{1,3}|\.(?:post|dev)[0-9]{1,3})?", re.ASCII)


def _exception_chain(error: BaseException | None) -> tuple[BaseException, ...]:
    # gTTS frequentemente encapsula Requests; Edge usa aiohttp. Contextos
    # implícitos também importam. O teto e os IDs visitados contêm ciclos e
    # cadeias patológicas sem copiar nenhuma mensagem privada de exceção.
    pending, seen, chain = [error], set(), []
    while pending and len(chain) < 16:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.append(current.__context__)
        pending.append(current.__cause__)
    return tuple(chain)


def _http_failure_code(error: BaseException) -> str | None:
    status = None
    response = None
    if isinstance(error, _REQUEST_HTTP_ERRORS):
        response = getattr(error, "response", None)
    elif type(error).__module__ == "gtts.tts" and type(error).__name__ == "gTTSError":
        response = getattr(error, "rsp", None)
    if isinstance(response, requests.Response):
        status = response.status_code
    elif isinstance(error, _AIOHTTP_RESPONSE_ERRORS):
        status = error.status
    elif isinstance(error, UrllibHTTPError):
        status = error.code
    # Não inferir um status de atributos de erro arbitrários, strings ou bools.
    if type(status) is not int:
        return None
    if status == 429:
        return "voice_rate_limited"
    if status in {401, 403}:
        return "voice_auth_failed"
    if 500 <= status <= 599:
        return "voice_service_failed"
    return None


def failure_code(reason_code: str, error: Exception | None = None) -> str:
    # Somente categorias conhecidas chegam ao chat; mensagens de exceção podem
    # conter texto de fala, caminhos, URLs de provedores ou credenciais.
    chain = _exception_chain(error)
    observed = set()
    for cause in chain:
        if type(cause).__module__ == "discord.opus" and type(cause).__name__ == "OpusNotLoaded":
            observed.add("opus_missing")
        elif isinstance(cause, _TLS_ERRORS):
            observed.add("tls_failed")
        elif isinstance(cause, _TIMEOUT_ERRORS):
            observed.add("network_timeout")
        elif isinstance(cause, _DNS_ERRORS):
            observed.add("dns_failed")
        elif (http_code := _http_failure_code(cause)) is not None:
            observed.add(http_code)
        elif isinstance(cause, _CONNECTION_ERRORS):
            observed.add("network_failed")
        elif type(cause).__module__ == "edge_tts.exceptions" and type(cause).__name__ == "NoAudioReceived":
            observed.add("no_audio_received")
        elif isinstance(cause, PermissionError):
            observed.add("storage_denied")
        elif isinstance(cause, FileNotFoundError):
            filename = str(getattr(cause, "filename", "") or "").replace("\\", "/").rsplit("/", 1)[-1]
            observed.add("ffmpeg_missing" if filename.lower() in {"ffmpeg", "ffmpeg.exe"} else "audio_missing")
    # Um ConnectionError externo de Requests pode encapsular TLS, DNS ou um
    # timeout. Preferir a evidência específica à categoria genérica de conexão.
    for code in (
        "opus_missing", "ffmpeg_missing", "storage_denied", "tls_failed",
        "network_timeout", "dns_failed", "voice_rate_limited", "voice_auth_failed",
        "voice_service_failed", "no_audio_received", "network_failed", "audio_missing",
    ):
        if code in observed:
            return code
    for cause in chain:
        code = getattr(cause, "code", None)
        if isinstance(code, str) and code in _MESSAGES:
            return code
    if any(isinstance(cause, _INTERNAL_ERRORS) for cause in chain):
        return "internal_error"
    return reason_code if reason_code in _MESSAGES else "tts_failed"


def _safe_runtime_versions() -> str:
    """Versões já em memória; sem importação, leitura de arquivo ou ambiente."""
    versions = []
    python = sys.version_info
    if isinstance(python, tuple) and len(python) >= 3:
        numbers = python[:3]
        if all(type(number) is int and 0 <= number <= 999 for number in numbers):
            versions.append("Python " + ".".join(str(number) for number in numbers))
    for module_name, label in (("gtts", "gTTS"), ("edge_tts", "Edge"), ("discord", "discord.py")):
        module = sys.modules.get(module_name)
        if type(module) is not ModuleType:
            continue
        version = module.__dict__.get("__version__")
        # Não chamar str() em objetos de flags, nem aceitar metadados livres.
        if type(version) is str and _VERSION.fullmatch(version):
            versions.append(f"{label} {version}")
    return " | ".join(versions)


def _safe_internal_location(error: BaseException) -> str:
    last = ""
    visited = set()
    # Usar o descritor nativo evita um __traceback__ substituto de subclasses.
    trace = BaseException.__traceback__.__get__(error, BaseException)
    for _ in range(64):
        if type(trace) is not TracebackType or id(trace) in visited:
            break
        visited.add(id(trace))
        frame, line = trace.tb_frame, trace.tb_lineno
        module_name = frame.f_globals.get("__name__")
        relative = _INTERNAL_FRAME_FILES.get(module_name) if type(module_name) is str else None
        filename, function = frame.f_code.co_filename, frame.f_code.co_name
        if relative is not None and type(filename) is str and type(function) is str:
            normalized = filename.replace("\\", "/")
            if (normalized == relative or normalized.endswith("/" + relative)) and _FUNCTION_NAME.fullmatch(function):
                if type(line) is int and 1 <= line <= 1_000_000:
                    last = f"{relative.rsplit('/', 1)[-1]}:{function}:{line}"
        trace = trace.tb_next
    return last


def internal_failure_details(error: BaseException | None) -> str:
    """Detalhes públicos restritos da mesma causa que justifica internal_error."""
    for cause in _exception_chain(error):
        canonical = next((kind.__name__ for kind in _INTERNAL_ERRORS if isinstance(cause, kind)), None)
        if canonical is None:
            continue
        details = [canonical]
        location = _safe_internal_location(cause)
        if location:
            details.append(location)
        versions = _safe_runtime_versions()
        return (" · ".join(details) + (" | " + versions if versions else ""))[:250]
    return ""


async def notify_tts_failure(cog, item, reason_code: str, error: Exception | None = None) -> None:
    """Aviso discreto vinculado ao prefixo original, sem conteúdo privado."""
    message_id = int(getattr(item, "message_id", 0) or 0)
    channel_id = int(getattr(item, "text_channel_id", 0) or 0)
    if not message_id or not channel_id:
        return
    try:
        guild_id, author_id = int(item.guild_id), int(item.author_id)
        guild = cog.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            return
        member = guild.get_member(author_id)
        if member is None:
            member = await guild.fetch_member(author_id)
        if not channel.permissions_for(member).view_channel:
            return
        if isinstance(channel, discord.Thread) and channel.is_private():
            if not channel.permissions_for(member).manage_threads:
                participant = await channel.fetch_member(author_id)
                if participant.id != author_id:
                    return
        me = guild.me
        permissions = channel.permissions_for(me)
        can_send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
        if not permissions.view_channel or not can_send:
            return

        code = failure_code(reason_code, error)
        permission = ""
        if reason_code in {"connection_failed", "voice_permission_missing", "voice_full"} or code in {"connection_failed", "voice_permission_missing", "voice_full"}:
            diagnosed = diagnose_voice_access(guild, _voice_channel_for_item(guild, item))
            if diagnosed is not None:
                code, permission = diagnosed
        category = _NOTICE_CATEGORY.get(code, code)
        if permission:
            category = f"{category}:{permission}"
        notices = getattr(cog, "_tts_failure_notice_times", None)
        if notices is None:
            notices = cog._tts_failure_notice_times = {}
        now = time.monotonic()
        key = (guild_id, author_id, category)
        if now - notices.get(key, float("-inf")) < 30.0:
            return
        for old_key, stamp in list(notices.items()):
            if now - stamp >= 60.0:
                notices.pop(old_key, None)
        if len(notices) >= 512:
            notices.pop(min(notices, key=notices.get), None)
        # Reserva antes do await para evitar múltiplas respostas simultâneas.
        notices[key] = now
        reference = discord.MessageReference(
            message_id=message_id, channel_id=channel_id, guild_id=guild_id, fail_if_not_exists=False,
        )
        try:
            await channel.send(
                "-# " + _public_notice(code, permission=permission), reference=reference,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            if notices.get(key) == now:
                notices.pop(key, None)
            raise
        # Logs restritos: detalhes seguros da causa, sem imprimir a fala.
        if code == "internal_error":
            log.warning("[tts_voice] falha interna reportada | guild=%s %s", guild_id, internal_failure_details(error))
        else:
            log.info("[tts_voice] aviso de falha | guild=%s code=%s", guild_id, code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[tts_voice] aviso de falha indisponível | type=%s", type(exc).__name__)
