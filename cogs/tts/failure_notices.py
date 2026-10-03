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

_MESSAGES = {
    "opus_missing": "O codec Opus não está disponível para reproduzir a fala.",
    "ffmpeg_missing": "O FFmpeg não está disponível para reproduzir a fala.",
    "no_frames": "O áudio gerado não produziu nenhum quadro de som.",
    "route_failed": "O destino de reprodução não confirmou a fala.",
    "synthesis_failed": "O serviço de voz não conseguiu gerar a fala agora.",
    "tls_failed": "Não consegui abrir uma conexão segura com o serviço de voz. Confira os certificados da instalação.",
    "network_timeout": "A operação de voz excedeu o tempo de espera.",
    "network_failed": "Não consegui conectar ao serviço de voz.",
    "dns_failed": "Não consegui resolver o endereço do serviço de voz.",
    "voice_rate_limited": "O serviço de voz limitou as solicitações. Tente novamente daqui a pouco.",
    "voice_auth_failed": "O serviço de voz recusou a autenticação ou o acesso.",
    "voice_service_failed": "O serviço de voz respondeu com uma falha do servidor.",
    "no_audio_received": "O serviço de voz respondeu sem enviar áudio.",
    "storage_denied": "Não consegui acessar o arquivo de áudio. Confira as permissões de armazenamento da instalação.",
    "internal_error": "Ocorreu um erro interno no processamento da fala.",
    "connection_failed": "Não consegui usar a conexão da call para falar.",
    "audio_missing": "O arquivo de áudio não estava disponível para reprodução.",
    "audio_source_failed": "Não consegui preparar o som para reprodução. Confira o FFmpeg e os codecs da instalação.",
    "playback_failed": "Não consegui reproduzir a fala na call.",
    "tts_failed": "Não consegui concluir essa fala.",
    "queue_unavailable": "O TTS está pausado ou sua fila está indisponível.",
    "bot_muted": "Estou silenciado na call. A staff precisa retirar meu silenciamento para eu falar.",
    "tts_disabled": "O TTS está desativado na instalação do bot.",
    "tts_guild_disabled": "O TTS está desativado neste servidor. A staff pode ativá-lo no painel de TTS.",
    "ignored_role": "Seu cargo está configurado para não usar TTS neste servidor.",
    "author_not_in_voice": "Entre em uma call para usar esse prefixo de fala.",
}


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
    """Responde apenas ao prefixo original e limita avisos repetidos por membro."""
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
        notices = getattr(cog, "_tts_failure_notice_times", None)
        if notices is None:
            notices = cog._tts_failure_notice_times = {}
        now = time.monotonic()
        key = (guild_id, author_id)
        if now - notices.get(key, float("-inf")) < 30.0:
            return
        for old_key, stamp in list(notices.items()):
            if now - stamp >= 60.0:
                notices.pop(old_key, None)
        if len(notices) >= 512:
            notices.pop(min(notices, key=notices.get), None)
        notices[key] = now
        code = failure_code(reason_code, error)
        details = internal_failure_details(error) if code == "internal_error" else ""
        engine = {"edge": "Edge", "gtts": "gTTS"}.get(str(getattr(item, "engine", "")), "TTS")
        reference = discord.MessageReference(
            message_id=message_id, channel_id=channel_id, guild_id=guild_id, fail_if_not_exists=False,
        )
        await channel.send(
            f"{engine}: {_MESSAGES[code]} Código: `{code}`{(' · ' + details) if details else ''}.", reference=reference,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[tts_voice] aviso de falha indisponível | type=%s", type(exc).__name__)
