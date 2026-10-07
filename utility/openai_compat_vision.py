"""Validação de imagens INLINE para a entrada OpenAI-compatible do Osaka.

A versão 0.0.111 do Off Grid AI envia image_url.url como data:image/...;base64,...
Não há download remoto neste gateway: aceitar URLs arbitrárias permitiria SSRF.
A validação de pixels/codec e a conversão são feitas depois, em thread limitada,
por cogs.chatbot.media, usando o mesmo pipeline de visão do chatbot Discord.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Any


_DATA_IMAGE = re.compile(r"\Adata:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})\Z", re.IGNORECASE)
MAX_INLINE_IMAGES = 3
MAX_IMAGE_INPUT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_IMAGE_INPUT_BYTES = 6 * 1024 * 1024


class InlineImageError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_inline_image_url(value: Any, *, max_bytes: int = MAX_IMAGE_INPUT_BYTES) -> bytes:
    """Decodifica exclusivamente data URLs de imagem com limite antes da alocação.

    Não grava os bytes em disco, não resolve hosts, não faz chamadas HTTP.
    O conteúdo decodificado precisa ainda passar no decoder Pillow (backend).
    """
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        raise InlineImageError("invalid_image_url")
    if not value.lower().startswith("data:image/"):
        raise InlineImageError("unsupported_image_source")
    # Evita executar regex e decoder num corpo desproporcional.
    if len(value) > 64 + ((max_bytes + 2) // 3) * 4:
        raise InlineImageError("image_too_large")
    match = _DATA_IMAGE.fullmatch(value)
    if not match:
        raise InlineImageError("invalid_image_url")
    encoded = match.group(2)
    if not encoded or len(encoded) % 4 != 0:
        raise InlineImageError("invalid_image_url")
    if (len(encoded.rstrip("=")) * 3 // 4) > max_bytes:
        raise InlineImageError("image_too_large")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InlineImageError("invalid_image_url") from exc
    if not decoded:
        raise InlineImageError("invalid_image_url")
    if len(decoded) > max_bytes:
        raise InlineImageError("image_too_large")
    # Rejeitar declaração de MIME incompatível antes de decodificar pixels.
    mime = match.group(1).lower()
    valid_signature = {
        "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
        "image/gif": decoded[:6] in (b"GIF87a", b"GIF89a"),
        "image/webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
    }
    if not valid_signature[mime]:
        raise InlineImageError("invalid_image_data")
    return decoded
