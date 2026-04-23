"""Áudio: STT (transcrição) via Groq Whisper + TTS (síntese) via edge-tts.

STT: dado um arquivo de áudio, devolve texto transcrito. Usado quando
user manda voice message no Discord.

TTS: dado um texto, devolve bytes de MP3. Usado quando user pede "fala
isso por áudio" ou quando o profile tem frequência de áudio configurada.

Deps externas:
- `edge-tts` (pip install edge-tts). Leve, sem API key, usa o serviço
  de TTS do Microsoft Edge. Suporta PT-BR com várias vozes.
- Whisper via HTTP, sem SDK (usamos aiohttp direto).
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


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
    if not audio_bytes:
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
                body = await resp.text()
                log.warning(
                    "chatbot: Whisper HTTP %s: %s",
                    resp.status, body[:200],
                )
                return None
            payload = await resp.json()
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

# Palavras-chave que indicam pedido EXPLÍCITO de resposta em áudio.
# Usadas só pra "user pediu" — a outra via (frequência do profile) é random.
_TTS_REQUEST_PATTERNS = (
    # pedidos diretos
    "responde por audio", "responde por áudio",
    "responde em audio", "responde em áudio",
    "manda audio", "manda áudio",
    "manda em audio", "manda em áudio",
    "fala por audio", "fala por áudio",
    "fala isso", "fala por voz",
    "me manda audio", "me manda áudio",
    "voz", "áudio",  # genérico — pode ter falsos positivos, mas ok no contexto
)


def user_asked_for_tts(text: str) -> bool:
    """True se a mensagem sugere que user quer resposta em áudio.

    É uma heurística simples — match de substring case-insensitive.
    Falsos positivos acontecem (ex: user escreve "ouvi o áudio que você
    mandou"), mas nesse caso o bot gerar áudio não é catastrófico.
    """
    if not text:
        return False
    lower = text.lower()
    # Evita matches triviais tipo só a palavra "voz" numa frase longa.
    # Exige que seja pedido direto, com verbos imperativos relevantes.
    strong = (
        "responde por audio", "responde por áudio",
        "responde em audio", "responde em áudio",
        "manda audio", "manda áudio",
        "me manda audio", "me manda áudio",
        "manda em audio", "manda em áudio",
        "fala por audio", "fala por áudio",
        "fala por voz",
    )
    return any(pat in lower for pat in strong)
