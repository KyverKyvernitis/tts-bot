from __future__ import annotations

import asyncio

from utility.image_recolor import normalize_icon_png, recolor_icon_bytes


async def normalize_original_icon(raw: bytes) -> bytes:
    return await asyncio.to_thread(normalize_icon_png, raw)


async def recolor_role_icon(raw: bytes, color_hex: str) -> bytes:
    return await asyncio.to_thread(recolor_icon_bytes, raw, color_hex)
