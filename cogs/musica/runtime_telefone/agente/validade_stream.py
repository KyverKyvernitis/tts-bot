"""Validade conservadora de URLs assinadas, independente do cache de metadata."""
from __future__ import annotations

import math
import time
from urllib.parse import parse_qs, urlsplit


def prazo_stream(
    url: str,
    resolved_at: float,
    fallback_seconds: float,
    *,
    max_age_seconds: float = 1800.0,
    margin_seconds: float = 60.0,
) -> float:
    """Só interpreta ``expire`` do CDN conhecido; outras origens usam TTL.

    O prazo assinado não garante disponibilidade: 403/queda de rede continuam
    invalidando a URL. O teto local limita a vida do cache mesmo com relógio ou
    assinatura inesperados, e nunca prolongamos uma assinatura vencida.
    """
    fallback = float(resolved_at) + max(0.0, float(fallback_seconds))
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host.endswith(".googlevideo.com"):
            return fallback
        expires = float(parse_qs(parsed.query).get("expire", [""])[0])
        if not math.isfinite(expires) or expires <= 0:
            return fallback
        signed = time.monotonic() + expires - time.time() - max(0.0, margin_seconds)
        return min(signed, float(resolved_at) + max(1.0, max_age_seconds))
    except (TypeError, ValueError, OverflowError):
        return fallback
