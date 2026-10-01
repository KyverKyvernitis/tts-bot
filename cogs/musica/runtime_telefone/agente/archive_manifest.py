"""Versioned Discord-only music archive manifests (post presentation v8).

The starter message owns the first audio attachment and ``archive-manifest.json``.
Each segment is an independently decodable audio file in that same thread.
Signed CDN URLs are deliberately excluded from the durable manifest.
"""
from __future__ import annotations

import hashlib
import base64
import zlib
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "archive-manifest.json"
MANIFEST_MARKER = "music-archive-v8-"
SEGMENT_MARKER = "music-archive-segment-v1:"
INLINE_FIELD_PREFIX = "Manifesto "
INLINE_FRAGMENT_PREFIX = "music-archive-data-v1="
_AUDIO_FIELDS = ("audio_codec", "audio_sample_rate", "audio_channels", "audio_stream_index", "audio_abr")
_REFERENCE_FIELDS = ("guild_id", "forum_id", "channel_id", "message_id", "attachment_id")


def encode_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _duration(raw: Any) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("duração inválida no manifesto")
    return value


def validate_manifest(raw: Any, *, archive_key: str, reference: dict) -> dict:
    """Validate content and require every segment to stay in the starter thread."""
    if not isinstance(raw, dict) or raw.get("version") != MANIFEST_VERSION or raw.get("archive_key") != archive_key:
        raise ValueError("versão ou identidade inválida no manifesto")
    duration = _duration(raw.get("duration"))
    if not re.fullmatch(r"[a-f0-9]{64}", str(raw.get("sha256") or "")):
        raise ValueError("hash inválido no manifesto")
    segments = raw.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segmentos ausentes no manifesto")
    normalized, offset, seen = [], 0.0, set()
    for order, entry in enumerate(segments):
        if not isinstance(entry, dict) or entry.get("order") != order:
            raise ValueError("ordem inválida no manifesto")
        ref = entry.get("reference")
        if not isinstance(ref, dict):
            raise ValueError("referência inválida no manifesto")
        ids = {field: int(ref[field]) for field in _REFERENCE_FIELDS}
        if any(value <= 0 for value in ids.values()) or any(ids[field] != int(reference[field]) for field in ("guild_id", "forum_id", "channel_id")):
            raise ValueError("segmento pertence a outro post")
        identity = (ids["message_id"], ids["attachment_id"])
        if identity in seen:
            raise ValueError("segmento duplicado no manifesto")
        seen.add(identity)
        if order == 0 and any(ids[field] != int(reference[field]) for field in ("message_id", "attachment_id")):
            raise ValueError("primeiro segmento diferente do arquivo")
        seconds = _duration(entry.get("duration"))
        start = float(entry.get("offset_seconds", -1))
        if not math.isfinite(start) or abs(start - offset) > 0.05:
            raise ValueError("offset inválido no manifesto")
        if not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256") or "")):
            raise ValueError("hash de segmento inválido")
        size = int(entry.get("size_bytes") or 0)
        filename = str(entry.get("filename") or "")
        if size <= 0 or not filename or len(filename) > 240 or "/" in filename or "\\" in filename:
            raise ValueError("arquivo de segmento inválido")
        technical = {}
        for field in _AUDIO_FIELDS:
            value = entry.get(field, raw.get(field))
            if field == "audio_codec":
                value = str(value or "").lower()
                if not re.fullmatch(r"[a-z0-9_]{1,40}", value):
                    raise ValueError("codec inválido no manifesto")
            else:
                value = int(value or 0)
                if value < 0 or (field in {"audio_sample_rate", "audio_channels"} and value <= 0):
                    raise ValueError("metadados técnicos inválidos no manifesto")
            technical[field] = value
        normalized.append({**entry, **technical, "reference": ids, "duration": seconds, "offset_seconds": start})
        offset += seconds
    if abs(offset - duration) > max(0.1, duration * 0.0001):
        raise ValueError("duração total diferente dos segmentos")
    first = normalized[0]
    if any(raw.get(field) != first[field] for field in _AUDIO_FIELDS):
        raise ValueError("perfil de áudio diferente do primeiro segmento")
    return {**raw, "duration": duration, "segments": normalized}


def flatten_segments(manifest: dict) -> list[dict]:
    return [{**entry["reference"], **{key: value for key, value in entry.items() if key != "reference"}}
            for entry in manifest["segments"]]


def inline_manifest(raw: dict) -> dict | None:
    if isinstance(raw.get("_archive_manifest"), dict):
        return raw["_archive_manifest"]
    embeds = raw.get("embeds") or []
    fragments = urlsplit(str(embeds[0].get("url") or "")).fragment.split("&") if embeds else []
    encoded = [fragment[len(INLINE_FRAGMENT_PREFIX):] for fragment in fragments if fragment.startswith(INLINE_FRAGMENT_PREFIX)]
    if encoded:
        if len(encoded) != 1 or len(encoded[0]) > 1400:
            raise ValueError("manifesto inline inválido")
        compressed = base64.b64decode(encoded[0] + "=" * (-len(encoded[0]) % 4), altchars=b"-_", validate=True)
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, 8 * 1024 * 1024 + 1)
        if len(decoded) > 8 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
            raise ValueError("manifesto inline inválido")
        manifest = json.loads(decoded)
        if not isinstance(manifest, dict):
            raise ValueError("manifesto inválido")
        return manifest
    fields = (embeds[0].get("fields") or []) if embeds and isinstance(embeds[0], dict) else []
    chunks = []
    for field in fields:
        name = str(field.get("name") or "")
        match = re.fullmatch(r"Manifesto (\d+)/(\d+)", name)
        if match:
            chunks.append((int(match[1]), int(match[2]), str(field.get("value") or "")))
    if not chunks:
        return None
    chunks.sort()
    count = chunks[0][1]
    if count != len(chunks) or [part[0] for part in chunks] != list(range(1, count + 1)) or any(part[1] != count for part in chunks):
        raise ValueError("manifesto incompleto no embed")
    value = json.loads("".join(part[2] for part in chunks))
    if not isinstance(value, dict):
        raise ValueError("manifesto inválido")
    return value


async def hydrate_archive_manifest(raw: dict, *, timeout: float = 15.0) -> dict:
    """Return the REST message with a manifest hydrated; legacy messages are unchanged.

    Small manifests are also inline, avoiding another CDN request on normal plays.
    Larger manifests are read from the starter's JSON attachment, bounded by the
    attachment size and an 8 MiB parser memory budget.
    """
    manifest = inline_manifest(raw)
    if manifest is not None:
        raw["_archive_manifest"] = manifest
        return raw
    embeds = raw.get("embeds") or []
    if not embeds or MANIFEST_MARKER not in str(embeds[0].get("url") or ""):
        return raw
    attachment = next((item for item in raw.get("attachments") or [] if item.get("filename") == MANIFEST_FILENAME), None)
    if not attachment:
        raise ValueError("manifesto do arquivo ainda não confirmado")
    size = int(attachment.get("size") or 0)
    if not 0 < size <= 8 * 1024 * 1024:
        raise ValueError("tamanho inválido do manifesto")
    url = str(attachment.get("url") or "")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"cdn.discordapp.com", "media.discordapp.net"} or parts.username or parts.password or parts.port:
        raise ValueError("URL inválida do manifesto")
    if not re.match(r"^/attachments/\d+/\d+/", parts.path):
        raise ValueError("URL inválida do manifesto")
    payload = bytearray()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError("manifesto indisponível no CDN")
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > size or len(payload) > 8 * 1024 * 1024:
                    raise ValueError("manifesto excedeu o tamanho confirmado")
    if len(payload) != size:
        raise ValueError("manifesto incompleto no CDN")
    manifest = json.loads(payload)
    if not isinstance(manifest, dict):
        raise ValueError("manifesto inválido")
    raw["_archive_manifest"] = manifest
    return raw


def add_inline_manifest(embed: Any, manifest: dict) -> None:
    """Keep small hot-path manifests in an opaque URL fragment, outside the UI."""
    compressed = zlib.compress(encode_manifest(manifest), level=6)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    parts = urlsplit(str(embed.url or ""))
    fragment = "&".join(piece for piece in parts.fragment.split("&") if not piece.startswith(INLINE_FRAGMENT_PREFIX))
    fragment = "&".join(filter(None, (fragment, INLINE_FRAGMENT_PREFIX + encoded)))
    candidate = urlunsplit(parts._replace(fragment=fragment))
    # Discord URLs are capped at 2,048 characters; leave space for the origin.
    if len(encoded) <= 1200 and len(candidate) <= 1950:
        embed.url = candidate
