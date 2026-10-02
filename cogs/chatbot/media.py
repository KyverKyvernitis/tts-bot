"""Utilitários de mídia: classificação de anexos, download, upload.

Este módulo centraliza o código que lida com os diferentes tipos de mídia
suportados pelo chatbot (imagem, áudio, voice msg). Todas as features de
mídia (visão, STT, TTS, imagegen) usam helpers daqui — evita duplicar
lógica de MIME/tamanho/etc em vários lugares.

Design: funções puras sempre que possível. Nada de estado. Se precisa de
sessão aiohttp pra baixar, é passada como argumento.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
import discord
from PIL import Image, ImageOps, UnidentifiedImageError

from . import constants as C

log = logging.getLogger(__name__)

# Downloads podem ocorrer em paralelo; só uma imagem ocupa memória decodificada
# por vez, inclusive quando a coroutine que a pediu já foi cancelada.
_IMAGE_PREPARATION_SLOTS = threading.BoundedSemaphore(1)


class ImagePreparationError(Exception):
    """Falha do anexo, sem URL assinada ou conteúdo nos diagnósticos."""

    def __init__(self, message: str, *, kind: str, status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.stage = "attachment"


@dataclass(frozen=True)
class PreparedImage:
    """Bytes validados, reutilizados em todos os modelos de visão."""

    mime_type: str
    data: bytes
    filename: str = ""
    first_frame_only: bool = False


def _image_url_allowed(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == domain or host.endswith("." + domain)
        for domain in ("discordapp.com", "discordapp.net", "discord.com")
    )


async def _download_image(
    session: aiohttp.ClientSession, attachment: MediaAttachment, *,
    deadline: float, limit: int,
) -> bytes:
    url = attachment.url
    if not _image_url_allowed(url):
        raise ImagePreparationError("origem do anexo inválida", kind="download")
    try:
        # O CDN pode redirecionar; cada destino permanece restrito ao Discord.
        for _ in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImagePreparationError("tempo de leitura do anexo esgotado", kind="timeout")
            timeout = aiohttp.ClientTimeout(
                total=remaining,
                connect=min(C.MEDIA_CONNECT_TIMEOUT_SECONDS, remaining),
                sock_read=min(C.MEDIA_READ_TIMEOUT_SECONDS, remaining),
            )
            async with session.get(url, timeout=timeout, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    destination = urljoin(url, response.headers.get("Location", ""))
                    if not response.headers.get("Location") or not _image_url_allowed(destination):
                        raise ImagePreparationError("redirecionamento do anexo inválido", kind="download")
                    url = destination
                    continue
                if response.status >= 400:
                    raise ImagePreparationError(
                        "não foi possível baixar o anexo", kind="download", status=response.status,
                    )
                mime = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if mime and mime not in C.SUPPORTED_IMAGE_MIMES and mime != "application/octet-stream":
                    raise ImagePreparationError("o anexo recebido não é uma imagem compatível", kind="mime")
                try:
                    declared = int(response.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0
                if declared > limit:
                    raise ImagePreparationError("o anexo ultrapassa o limite de leitura", kind="size")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ImagePreparationError("o anexo ultrapassa o limite de leitura", kind="size")
                if not data:
                    raise ImagePreparationError("o anexo está vazio", kind="unreadable")
                return bytes(data)
        raise ImagePreparationError("redirecionamentos demais no anexo", kind="download")
    except asyncio.TimeoutError as exc:
        raise ImagePreparationError("tempo de leitura do anexo esgotado", kind="timeout") from exc
    except aiohttp.ClientError as exc:
        raise ImagePreparationError("falha de rede ao baixar o anexo", kind="download") from exc


def _prepare_image_bytes(
    data: bytes, filename: str, *, byte_limit: int, deadline: Optional[float] = None,
) -> PreparedImage:
    """O worker mantém o slot até liberar os pixels, independentemente do caller."""
    if deadline is None:
        acquired = _IMAGE_PREPARATION_SLOTS.acquire()
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImagePreparationError("tempo de preparo da imagem esgotado", kind="timeout")
        acquired = _IMAGE_PREPARATION_SLOTS.acquire(timeout=remaining)
    if not acquired:
        raise ImagePreparationError("tempo de preparo da imagem esgotado", kind="timeout")
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise ImagePreparationError("tempo de preparo da imagem esgotado", kind="timeout")
        return _decode_image_bytes(data, filename, byte_limit=byte_limit)
    finally:
        _IMAGE_PREPARATION_SLOTS.release()


def _decode_image_bytes(data: bytes, filename: str, *, byte_limit: int) -> PreparedImage:
    """Decodifica antes de enviar; PNG preserva texto e JPEG limita fotos grandes."""
    max_pixels = getattr(C, "MAX_VISION_IMAGE_PIXELS", 40_000_000)
    max_side = getattr(C, "MAX_VISION_IMAGE_SIDE", 4096)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise ImagePreparationError("formato de imagem incompatível", kind="mime")
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise ImagePreparationError("a imagem possui pixels demais", kind="size")
                animated = bool(getattr(source, "is_animated", False))
                source.seek(0)
                image = ImageOps.exif_transpose(source)
                # Fundo branco mantém texto legível e é compatível com ambos os providers.
                if "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = image.convert("RGB")
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                # Texto em screenshots deve continuar sem artefatos quando couber.
                prefer_png = source.format in {"PNG", "GIF"}
                for attempt in range(8):
                    output = io.BytesIO()
                    use_png = prefer_png and attempt == 0
                    if use_png:
                        image.save(output, format="PNG", optimize=True)
                    else:
                        image.save(output, format="JPEG", quality=92 if attempt < 2 else 85, optimize=True)
                    encoded = output.getvalue()
                    if len(encoded) <= byte_limit:
                        return PreparedImage(
                            "image/png" if use_png else "image/jpeg", encoded,
                            filename=filename, first_frame_only=animated,
                        )
                    if not use_png:
                        image.thumbnail(
                            (max(1, int(image.width * .8)), max(1, int(image.height * .8))),
                            Image.Resampling.LANCZOS,
                        )
        raise ImagePreparationError("não foi possível reduzir a imagem ao limite", kind="size")
    except ImagePreparationError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImagePreparationError("a imagem possui pixels demais", kind="size") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImagePreparationError("não foi possível decodificar a imagem", kind="unreadable") from exc


async def prepare_image_attachments(
    session: aiohttp.ClientSession, attachments: list[MediaAttachment], *,
    timeout_seconds: Optional[float] = None,
) -> list[PreparedImage]:
    """Baixa uma vez, com limites por imagem e por lote e um único prazo."""
    deadline = time.monotonic() + (
        C.MEDIA_READ_TIMEOUT_SECONDS if timeout_seconds is None else max(.01, timeout_seconds)
    )
    input_budget = getattr(C, "MAX_VISION_INPUT_TOTAL_BYTES", 40 * 1024 * 1024)
    output_budget = getattr(C, "MAX_VISION_TOTAL_BYTES", 12 * 1024 * 1024)
    prepared: list[PreparedImage] = []
    seen: set[str] = set()
    for attachment in attachments:
        if attachment.url in seen:
            continue
        seen.add(attachment.url)
        if len(prepared) >= C.MAX_IMAGES_PER_MESSAGE:
            break
        mime = attachment.mime_type.split(";", 1)[0].strip().lower()
        if mime and mime not in C.SUPPORTED_IMAGE_MIMES:
            raise ImagePreparationError("formato de imagem incompatível", kind="mime")
        limit = min(C.MAX_IMAGE_SIZE_BYTES, input_budget)
        if limit <= 0 or attachment.size_bytes > limit:
            raise ImagePreparationError("os anexos ultrapassam o limite de leitura", kind="size")
        data = await _download_image(session, attachment, deadline=deadline, limit=limit)
        input_budget -= len(data)
        byte_limit = min(C.MAX_GEMINI_IMAGE_BYTES, output_budget)
        if byte_limit <= 0:
            raise ImagePreparationError("os anexos ultrapassam o limite de envio", kind="size")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            image = await asyncio.wait_for(
                asyncio.to_thread(
                    _prepare_image_bytes, data, attachment.filename,
                    byte_limit=byte_limit, deadline=deadline,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise ImagePreparationError("tempo de preparo da imagem esgotado", kind="timeout") from exc
        prepared.append(image)
        output_budget -= len(image.data)
    return prepared


def channel_is_nsfw(channel) -> bool:
    """Threads e posts de fórum herdam a restrição de idade do canal pai."""
    predicate = getattr(channel, "is_nsfw", None)
    if callable(predicate):
        return bool(predicate())
    return bool(getattr(channel, "nsfw", False))


@dataclass(frozen=True)
class MediaAttachment:
    """Metadados aceitos; visão ainda valida e prepara os bytes antes do envio."""
    url: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str  # "image" | "audio"


def classify_attachment(att: discord.Attachment) -> Optional[MediaAttachment]:
    """Classifica um anexo do Discord. Retorna None se não é processável.

    Regras:
    - MIME precisa estar nas listas suportadas
    - Tamanho dentro do limite do provider (imagens 20MB, áudio 25MB)
    - Se qualquer checagem falha, retorna None (caller ignora silenciosamente)

    Não levanta exceção — anexos não processáveis são esperados (ex: user
    manda um PDF, um .txt, etc) e devem ser ignorados sem barulho.
    """
    mime = (att.content_type or "").split(";", 1)[0].lower().strip()
    if not mime or mime == "application/octet-stream":
        # Algumas vezes content_type vem vazio. Tenta inferir pela extensão.
        ext = (att.filename or "").lower().rsplit(".", 1)[-1] if "." in (att.filename or "") else ""
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif",
            "ogg": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
            "wav": "audio/wav", "flac": "audio/flac", "opus": "audio/ogg",
        }.get(ext, "")

    size = int(att.size or 0)

    if mime in C.SUPPORTED_IMAGE_MIMES:
        if size > C.MAX_IMAGE_SIZE_BYTES:
            log.info("media: imagem %s muito grande (%s bytes)", att.filename, size)
            return None
        return MediaAttachment(
            url=att.url,
            filename=att.filename or "image",
            mime_type=mime,
            size_bytes=size,
            kind="image",
        )

    if mime in C.SUPPORTED_AUDIO_MIMES:
        if size > C.MAX_AUDIO_SIZE_BYTES:
            log.info("media: áudio %s muito grande (%s bytes)", att.filename, size)
            return None
        return MediaAttachment(
            url=att.url,
            filename=att.filename or "audio",
            mime_type=mime,
            size_bytes=size,
            kind="audio",
        )

    return None


def extract_attachments(message: discord.Message) -> tuple[list[MediaAttachment], list[MediaAttachment]]:
    """Extrai todos os anexos processáveis de uma mensagem.

    Retorna (imagens, áudios) separados. Já filtrados por
    MIME/tamanho/limite. Imagens são cortadas a MAX_IMAGES_PER_MESSAGE.
    """
    images: list[MediaAttachment] = []
    audios: list[MediaAttachment] = []
    for att in message.attachments:
        classified = classify_attachment(att)
        if classified is None:
            continue
        if classified.kind == "image":
            if len(images) < C.MAX_IMAGES_PER_MESSAGE:
                images.append(classified)
        elif classified.kind == "audio":
            audios.append(classified)
    return images, audios


def is_voice_message(message: discord.Message) -> bool:
    """True se a mensagem é uma voice note do Discord (não um áudio anexado).

    Discord marca voice notes com flag. Essas são diferentes de user-attached
    audio files — geralmente .ogg curtos, gravados no cliente. A distinção
    é útil pra UX (voice note = user "falando", trigger por default).
    """
    # `is_voice_message` é atributo do MessageFlags em discord.py 2.4+
    flags = getattr(message, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "voice", False))


async def download_attachment_bytes(
    session: aiohttp.ClientSession,
    media: MediaAttachment,
    *,
    max_bytes: Optional[int] = None,
) -> Optional[bytes]:
    """Baixa os bytes de um anexo pra processar localmente.

    Usado quando precisamos enviar o arquivo pro provider (ex: Whisper STT,
    que aceita multipart/form). Visão usa prepare_image_attachments para
    validar os pixels e reutilizar os mesmos bytes entre providers.

    Retorna None se o download falhar ou exceder `max_bytes`.
    """
    limit = max_bytes or media.size_bytes
    timeout = aiohttp.ClientTimeout(
        total=C.MEDIA_READ_TIMEOUT_SECONDS,
        connect=C.MEDIA_CONNECT_TIMEOUT_SECONDS,
        sock_read=C.MEDIA_READ_TIMEOUT_SECONDS,
    )
    try:
        async with session.get(media.url, timeout=timeout) as resp:
            if resp.status >= 400:
                log.warning("media: download de anexo falhou HTTP %s", resp.status)
                return None
            # Lê com cap pra proteger memória
            buf = io.BytesIO()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    log.warning("media: download de anexo passou do limite %s", limit)
                    return None
            return buf.getvalue()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("media: erro de rede ao baixar anexo (%s)", type(e).__name__)
        return None
