"""Áudio: STT (transcrição) via Groq Whisper + TTS (síntese) via edge-tts.

STT: dado um arquivo de áudio, devolve texto transcrito. Usado quando
user manda voice message no Discord.

TTS: dado um texto, devolve bytes de MP3 quando o usuário pede uma
resposta em áudio.

Deps externas:
- `edge-tts` (pip install edge-tts). Leve, sem API key, usa o serviço
  de TTS do Microsoft Edge. Suporta PT-BR com várias vozes.
- Whisper via HTTP, sem SDK (usamos aiohttp direto).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import unicodedata
from typing import Optional

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


async def _read_response_capped(response, limit: int) -> Optional[bytes]:
    data = bytearray()
    async for chunk in response.content.iter_chunked(16 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            return None
    return bytes(data)


# -----------------------------------------------------------------------------
# STT — Speech-to-text via Groq Whisper
# -----------------------------------------------------------------------------

async def transcribe_audio(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    audio_bytes: bytes,
    filename: str = "audio.ogg",
    language: Optional[str] = "pt",
) -> Optional[str]:
    """Transcreve áudio via Groq Whisper.

    Retorna o texto transcrito, ou None se falhou. Exceções de rede são
    tratadas — nunca levanta.

    Args:
        audio_bytes: conteúdo do arquivo (já baixado)
        filename: nome do arquivo (extensão importa pro content-type)
        language: ISO-639-1 do idioma esperado (melhora accuracy + latência).
                  Default 'pt' pro nosso bot. Passar None deixa Whisper detectar.
    """
    if not audio_bytes or len(audio_bytes) > C.MAX_AUDIO_SIZE_BYTES:
        return None

    data = aiohttp.FormData()
    data.add_field("file", audio_bytes, filename=filename)
    data.add_field("model", C.GROQ_WHISPER_MODEL)
    data.add_field("response_format", "json")
    if language:
        data.add_field("language", language)

    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=C.PROVIDER_TIMEOUT_SECONDS)

    try:
        async with session.post(
            C.GROQ_WHISPER_URL,
            data=data,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                body_bytes = await _read_response_capped(resp, 4096)
                body = (body_bytes or b"").decode("utf-8", errors="replace")
                log.warning(
                    "chatbot: Whisper HTTP %s: %s",
                    resp.status, body[:200],
                )
                return None
            payload_bytes = await _read_response_capped(resp, 256 * 1024)
            if payload_bytes is None:
                raise ValueError("resposta Whisper grande demais")
            payload = json.loads(payload_bytes.decode("utf-8"))
            text = str(payload.get("text") or "").strip()
            if not text:
                return None
            return text
    except asyncio.TimeoutError:
        log.warning("chatbot: Whisper timeout após %ss", C.PROVIDER_TIMEOUT_SECONDS)
        return None
    except aiohttp.ClientError as e:
        log.warning("chatbot: Whisper erro de rede: %s", e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("chatbot: Whisper resposta malformada: %s", e)
        return None


# -----------------------------------------------------------------------------
# TTS — Text-to-speech via edge-tts (Microsoft Edge)
# -----------------------------------------------------------------------------

# Voz default — pt-BR feminina neutra. Edge tem dezenas. Lista completa:
# `edge-tts --list-voices | grep pt-BR`. Algumas opções:
#   pt-BR-FranciscaNeural (default, feminina adulta)
#   pt-BR-AntonioNeural (masculina adulta)
#   pt-BR-BrendaNeural, pt-BR-DonatoNeural, etc
DEFAULT_TTS_VOICE = "pt-BR-FranciscaNeural"

# Limite de tamanho do texto — TTS pode demorar e gerar arquivo grande.
# Respostas do bot são tipicamente curtas; cap defensivo.
MAX_TTS_CHARS = 800


async def synthesize_speech(
    text: str,
    *,
    voice: str = DEFAULT_TTS_VOICE,
) -> Optional[bytes]:
    """Gera MP3 falando `text`. Retorna bytes ou None se falhou.

    Depende de `edge_tts` estar instalado. Se não estiver, retorna None
    com log — o cog usa isso como "TTS indisponível" e segue com texto.

    Roda a síntese em run_in_executor porque edge_tts é sincrono por trás
    (embora exponha API async — ele bloqueia no socket interno).
    """
    if not text:
        return None
    # Trim pra não gerar áudio gigante
    text = text.strip()[:MAX_TTS_CHARS]

    try:
        import edge_tts  # type: ignore
    except ImportError:
        log.warning("chatbot: edge_tts não instalado — TTS indisponível")
        return None

    try:
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf.write(chunk.get("data", b""))
                if buf.tell() > C.MAX_TTS_OUTPUT_BYTES:
                    log.warning("chatbot: TTS excedeu limite de bytes")
                    return None
        data = buf.getvalue()
        if not data:
            return None
        return data
    except Exception as e:
        # edge_tts levanta tipos internos; pegamos tudo pra não crashar
        log.warning("chatbot: TTS falhou: %s", e)
        return None


# -----------------------------------------------------------------------------
# Detecção de pedido de áudio no texto do user
# -----------------------------------------------------------------------------

# Aceita imperativos formais e informais sem tratar qualquer menção a áudio
# como pedido. Infinitivos ficam restritos a pedidos como "pode enviar".
_TTS_COMMAND = (
    r"(?:manda|mande|envia|envie|responde|responda|fala|fale|diz|diga|"
    r"solta|solte|gera|gere|cria|crie|faz|faca|grava|grave)"
)
_TTS_REQUEST_VERB = (
    r"(?:mandar|enviar|responder|falar|dizer|soltar|gerar|criar|fazer|gravar|"
    + _TTS_COMMAND + r")"
)
_TTS_DIRECT_AUDIO_OBJECT = (
    r"(?:(?:mais|isso)\s+)?(?:(?:em|por|com)\s+)?"
    r"(?:(?:um|uma|outro|outra)\s+)?(?:audio|voz)\b"
)
_TTS_AUDIO_OBJECT = (
    _TTS_DIRECT_AUDIO_OBJECT
    + r"|(?:isso|algo|alguma coisa|qualquer coisa|"
    r"(?:a|essa|esta|sua) resposta|(?:a|minha|essa) pergunta|"
    r"(?:essa|esta) mensagem)\s+(?:em|por|com)\s+(?:audio|voz)\b"
)
_TTS_REQUEST_RE = re.compile(
    r"(?:^|[.!?;]\s*)(?:por favor[,]?\s+)?(?:me\s+)?"
    + _TTS_COMMAND + r"\s+(?:" + _TTS_AUDIO_OBJECT + r")"
    r"|(?:^|[.!?;]\s*)(?:voce\s+)?(?:pode|poderia|consegue|conseguiria|"
    r"quero|queria|gostaria de)\s+(?:me\s+)?"
    + _TTS_REQUEST_VERB + r"\s+(?:" + _TTS_AUDIO_OBJECT + r")"
    r"|^(?:quero|queria|gostaria de)\s+(?:" + _TTS_DIRECT_AUDIO_OBJECT + r")"
    r"|^(?:gerar|criar|fazer|gravar)\s+(?:" + _TTS_AUDIO_OBJECT + r")"
    r"|^(?:fala|fale|diz|diga)\s+isso\b"
    r"|^(?:audio|voz)\s+(?:disso|dessa resposta|da resposta)\b"
)
_TTS_NEGATIVE_RE = re.compile(
    r"\bsem\s+(?:audio|voz)\b"
    r"|\b(?:nao|nunca|nem)\s+(?:me\s+)?(?:"
    + _TTS_REQUEST_VERB
    + r"|quero|queria|preciso|precisa|precisamos)\s+"
    r"(?:\w+\s+){0,8}(?:audio|voz|isso)\b"
)


def user_asked_for_tts(text: str) -> bool:
    """True se a mensagem sugere que user quer resposta em áudio.

    Reconhece pedidos diretos ("mande um áudio", "fale em áudio") e respostas
    curtas ("em áudio"), respeitando negativas e evitando frases narrativas.
    """
    if not text:
        return False

    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(char)
    )
    normalized = re.sub(r"<@!?\d+>", " ", normalized)
    normalized = " ".join(normalized.split())
    if not normalized:
        return False

    if _TTS_NEGATIVE_RE.search(normalized):
        return False

    # Mensagens curtíssimas em reply, tipo "em áudio" ou "por voz".
    if re.fullmatch(
        r"(?:(?:em|por)\s+)?(?:audio|voz)(?:[,!.?]\s*)?(?:por favor)?[!.?]?",
        normalized,
    ):
        return True

    return _TTS_REQUEST_RE.search(normalized) is not None
