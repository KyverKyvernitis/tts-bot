"""Geração de imagem via Gemini 2.5 Flash Image.

Endpoint REST, payload JSON, response tem a imagem em base64 no campo
`inlineData.data` de algum `part` dentro da primeira candidate.

Trigger pode ser explícito (comando `/chatbot imagem <prompt>`) ou implícito
(user escreve algo que parece pedido — "gera uma imagem de X", "desenha Y").
O módulo expõe detecção + geração, o cog decide quando chamar.

Importante: a resposta pode demorar 10-30s. O cog deve reagir primeiro
(ex: emoji "processando") pra o user saber que ta gerando.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeneratedImage:
    """Resultado de uma geração bem-sucedida."""
    data: bytes          # bytes da imagem (PNG ou JPEG)
    mime_type: str       # "image/png" geralmente
    caption: Optional[str] = None  # texto opcional que o modelo gerou junto


# -----------------------------------------------------------------------------
# Detecção de pedido implícito de imagem no texto do user
# -----------------------------------------------------------------------------

# Regex: frases que parecem pedido de gerar imagem.
# Exige verbo imperativo + palavras relacionadas. Evita falsos positivos
# tipo "vi uma imagem legal ontem".
_IMAGE_REQUEST_RE = re.compile(
    r"\b("
    r"gera(r)?\s+(uma\s+|um\s+)?(imagem|foto|figura|desenho|arte|ilustra..o)"
    r"|desenha(r)?\s+"
    r"|(faz|faça|faca|faca|faz[ae])\s+(uma\s+|um\s+)?(imagem|desenho|arte|figura|ilustra..o)"
    r"|me\s+mostra\s+(uma\s+|um\s+)?(imagem|desenho)"
    r"|cria\s+(uma\s+|um\s+)?(imagem|foto|arte|ilustra..o)"
    r"|imagina\s+(uma\s+|um\s+)?(cena|imagem)"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def detect_image_request(text: str) -> bool:
    """True se o texto parece pedir geração de imagem.

    É uma heurística — pode errar. Por isso o cog pode gerar só quando
    o pedido é CLARO (ativo por default) e permitir override via comando
    explícito pros casos ambíguos.
    """
    if not text:
        return False
    return bool(_IMAGE_REQUEST_RE.search(text))


def extract_image_prompt(text: str) -> str:
    """Extrai o que o user quer que seja desenhado.

    Estratégia simples: remove o verbo imperativo + "imagem de" e deixa o
    resto. Ex: "desenha um dragão azul" → "um dragão azul".

    Se não conseguir extrair bem, retorna o texto completo (o modelo de
    imagem costuma aguentar verbosidade).
    """
    if not text:
        return ""
    # Remove prefixos comuns
    text = text.strip()
    patterns = [
        r"^(gera|desenha|cria|faz|faça|imagina|me mostra)\s+",
        r"^(uma|um)\s+",
        r"^(imagem|foto|figura|desenho|arte|ilustra[cç][aã]o|cena)\s+(de\s+|com\s+)?",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
    return text or "uma imagem"


# -----------------------------------------------------------------------------
# Geração via Gemini
# -----------------------------------------------------------------------------

async def generate_image(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    prompt: str,
    timeout_seconds: float = 45.0,
) -> Optional[GeneratedImage]:
    """Gera uma imagem a partir do prompt. Retorna None se falhou.

    Usa o Gemini 2.5 Flash Image. Timeout maior que chat porque imagem demora.
    Nunca levanta — falhas viram None + log.
    """
    if not prompt or not prompt.strip():
        return None
    if not api_key:
        log.warning("chatbot: imagegen sem GEMINI_API_KEY")
        return None

    url = C.GEMINI_IMAGEGEN_URL.format(model=C.GEMINI_IMAGEGEN_MODEL)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt.strip()[:2000]}],
            }
        ],
        # A documentação mostra configurar responseModalities=["IMAGE"] pra
        # forçar resposta com imagem. Sem isso o modelo pode responder com texto.
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.warning(
                    "chatbot: imagegen HTTP %s: %s",
                    resp.status, body[:300],
                )
                return None
            data = await resp.json()
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen erro de rede: %s", e)
        return None
    except Exception as e:
        log.warning("chatbot: imagegen erro inesperado: %s", e)
        return None

    # Parse: busca inlineData no primeiro candidate
    try:
        candidates = data.get("candidates") or []
        if not candidates:
            log.warning("chatbot: imagegen sem candidates")
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
    except (AttributeError, TypeError):
        log.warning("chatbot: imagegen resposta malformada")
        return None

    caption_parts: list[str] = []
    image_data: Optional[bytes] = None
    image_mime = "image/png"
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part and part["text"]:
            caption_parts.append(str(part["text"]))
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and isinstance(inline, dict):
            b64 = inline.get("data")
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            if b64:
                try:
                    image_data = base64.b64decode(b64)
                    image_mime = mime
                except (ValueError, TypeError) as e:
                    log.warning("chatbot: imagegen falha decodificar base64: %s", e)
                    continue

    if image_data is None:
        log.info("chatbot: imagegen — modelo respondeu sem imagem (talvez safety block)")
        return None

    caption = " ".join(caption_parts).strip() or None
    return GeneratedImage(
        data=image_data,
        mime_type=image_mime,
        caption=caption,
    )
