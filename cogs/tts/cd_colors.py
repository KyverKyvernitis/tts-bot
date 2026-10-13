"""Cheap, bounded album-cover accents for Discord CD panels.

Follows the pixel-selection approach of the welcome avatar renderer: convert
an image to a tiny RGBA grid and discount transparent / nearly-black / nearly-
white backgrounds. Grayscale art stays grayscale.
"""
from __future__ import annotations

import hashlib
import io

DEFAULT_COLOR = 0x747474


def _extract(source: bytes) -> int:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(source)) as picture:
        if not (0 < picture.width <= 2048 and 0 < picture.height <= 2048):
            raise ValueError("invalid_cd_cover_dimensions")
        pixels = list(ImageOps.fit(picture.convert("RGBA"), (32, 32)).getdata())
        samples = [(r, g, b) for r, g, b, a in pixels if a >= 90]
    if not samples:
        return DEFAULT_COLOR
    # White background is common on art; preserve monochrome when color is absent.
    chromatic = [p for p in samples if max(p) - min(p) >= 18 and 32 <= max(p) <= 244]
    if chromatic:
        # Favor the most common saturated colored region rather than a stray pixel.
        buckets: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
        for r, g, b in chromatic:
            key = (r // 32, g // 32, b // 32)
            buckets.setdefault(key, []).append((r, g, b))
        group = max(buckets.values(), key=len)
    else:
        group = [p for p in samples if 25 < sum(p) / 3 < 235] or samples
    result = tuple(round(sum(p[i] for p in group) / len(group)) for i in range(3))
    if not chromatic:
        v = round(sum(result) / 3)
        result = (v, v, v)
    return (result[0] << 16) | (result[1] << 8) | result[2]


_COLORS: dict[str, int] = {}


def cover_accent(data: bytes | None, *, fallback: int = DEFAULT_COLOR) -> int:
    if not data:
        return fallback
    try:
        identity = hashlib.sha256(data).hexdigest()
        if identity not in _COLORS:
            _COLORS[identity] = _extract(data)
            if len(_COLORS) > 96:
                _COLORS.pop(next(iter(_COLORS)))
        return _COLORS[identity]
    except (ValueError, OSError, ImportError):
        return fallback
