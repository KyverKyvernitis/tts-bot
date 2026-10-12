"""Versioned Discord-only music archive manifests (post presentation v8).

The starter message owns the first audio attachment and ``archive-manifest.json``.
Each segment is an independently decodable audio file in that same thread.
Signed CDN URLs are deliberately excluded from the durable manifest.
"""
from __future__ import annotations

import hashlib
import copy
import time
from collections import OrderedDict
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


def file_sha256(path: Path, *, checkpoint=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if checkpoint:
                checkpoint()
            block = source.read(1024 * 1024)
            if not block:
                break
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


class ArchiveManifestCache:
    """Bounded RAM cache for validated, URL-free immutable manifest revisions.

    A fresh Discord REST message still verifies ownership and attachments before
    reusing a revision. CDN signatures never form part of this cache or its key.
    """
    def __init__(self, *, max_entries: int = 128, max_bytes: int = 4 * 1024 * 1024,
                 ttl_seconds: float = 86400.0) -> None:
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries: OrderedDict[tuple, tuple[float, dict, int]] = OrderedDict()
        self._bytes = 0

    @staticmethod
    def revision(raw: dict, *, archive_key: str, reference: dict, bot_id: int) -> tuple:
        if str((raw.get("author") or {}).get("id")) != str(bot_id):
            raise ValueError("manifesto não pertence ao bot")
        for name in ("guild_id", "channel_id"):
            if str(raw.get(name) or reference[name]) != str(reference[name]):
                raise ValueError("manifesto pertence a outro post")
        if raw.get("id") and str(raw["id"]) != str(reference["message_id"]):
            raise ValueError("mensagem do manifesto diferente do arquivo")
        embeds = raw.get("embeds") or []
        embed = embeds[0] if embeds and isinstance(embeds[0], dict) else {}
        markers = [part for part in urlsplit(str(embed.get("url") or "")).fragment.split("&")
                   if re.match(r"music-archive-v[2-8]-", part)]
        if markers != [MANIFEST_MARKER + archive_key] or str((embed.get("footer") or {}).get("text")) != "Arquivo de músicas":
            raise ValueError("identificador do manifesto diferente do arquivo")
        attachments = raw.get("attachments") or []
        manifest_attachment = next((item for item in attachments if isinstance(item, dict)
                                    and item.get("filename") == MANIFEST_FILENAME), None)
        if not manifest_attachment:
            raise ValueError("manifesto do arquivo ainda não confirmado")
        manifest_id = int(manifest_attachment.get("id") or 0)
        expected = int(reference.get("manifest_attachment_id") or manifest_id)
        if manifest_id <= 0 or manifest_id != expected:
            raise ValueError("anexo do manifesto diferente do arquivo")
        # Public message metadata is part of the revision, including the first
        # audio's name and size. Refreshing a CDN signature alone keeps it valid.
        identity = {"edited_timestamp": raw.get("edited_timestamp"),
                    "embed": {field: embed.get(field) for field in ("url", "footer", "description", "fields")},
                    "attachments": [{field: item.get(field) for field in ("id", "filename", "size")}
                                    for item in attachments if isinstance(item, dict)]}
        fingerprint = hashlib.sha256(encode_manifest(identity)).hexdigest()
        return (int(bot_id), *(int(reference.get(field) or 0) for field in _REFERENCE_FIELDS),
                manifest_id, archive_key, fingerprint)

    def get(self, revision: tuple) -> dict | None:
        entry = self._entries.get(revision)
        if entry is None:
            return None
        created, manifest, size = entry
        if time.monotonic() - created >= self.ttl_seconds:
            self._entries.pop(revision)
            self._bytes -= size
            return None
        self._entries.move_to_end(revision)
        return copy.deepcopy(manifest)

    def put(self, revision: tuple, manifest: dict) -> None:
        # Whitelist durable audio metadata: no arbitrary extras or signed URLs.
        stable = {field: manifest[field] for field in
                  ("version", "archive_key", "duration", "sha256", *_AUDIO_FIELDS)}
        stable["segments"] = [{field: entry[field] for field in
                               ("order", "offset_seconds", "duration", "sha256", "size_bytes", "filename", "reference", *_AUDIO_FIELDS)}
                              for entry in manifest["segments"]]
        stable = copy.deepcopy(stable)
        size = 3 * len(encode_manifest(stable)) + 512
        old = self._entries.pop(revision, None)
        if old is not None:
            self._bytes -= old[2]
        if not self.max_entries or self.ttl_seconds <= 0 or size > self.max_bytes:
            return
        while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
            _, removed = self._entries.popitem(last=False)
            self._bytes -= removed[2]
        self._entries[revision] = (time.monotonic(), stable, size)
        self._bytes += size


async def _download_manifest(attachment: dict, raw: dict, *, timeout: float,
                             session: aiohttp.ClientSession | None) -> dict:
    size = int(attachment.get("size") or 0)
    if not 0 < size <= 8 * 1024 * 1024:
        raise ValueError("tamanho inválido do manifesto")
    url = str(attachment.get("url") or "")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"cdn.discordapp.com", "media.discordapp.net"} or parts.username or parts.password or parts.port:
        raise ValueError("URL inválida do manifesto")
    path = re.match(r"^/attachments/(\d+)/(\d+)/[^/]+$", parts.path)
    if not path or str(path[2]) != str(attachment.get("id")) or (raw.get("channel_id") and str(path[1]) != str(raw["channel_id"])):
        raise ValueError("URL do manifesto diferente do anexo confirmado")
    payload = bytearray()

    async def download(client: aiohttp.ClientSession) -> None:
        async with client.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise ValueError("manifesto indisponível no CDN")
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > size or len(payload) > 8 * 1024 * 1024:
                    raise ValueError("manifesto excedeu o tamanho confirmado")

    if session is None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as temporary:
            await download(temporary)
    else:
        await download(session)
    if len(payload) != size:
        raise ValueError("manifesto incompleto no CDN")
    manifest = json.loads(payload)
    if not isinstance(manifest, dict):
        raise ValueError("manifesto inválido")
    return manifest


async def hydrate_archive_manifest(raw: dict, *, timeout: float = 15.0,
                                   session: aiohttp.ClientSession | None = None,
                                   metadata_cache: ArchiveManifestCache | None = None,
                                   archive_key: str = "", reference: dict | None = None,
                                   bot_id: int | None = None) -> dict:
    """Hydrate a v8 manifest, pooling HTTP and reusing trusted metadata if supplied.

    Small inline manifests and legacy messages do not open an HTTP session.
    Every cache hit requires a current authenticated Discord REST message with
    the same immutable attachment revision. Audio and CDN URLs stay uncached
    here; the resolver maintains their separate short-lived cache.
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
    revision = None
    if metadata_cache is not None:
        if not archive_key or reference is None or bot_id is None:
            raise ValueError("contexto autenticado ausente para o manifesto")
        revision = metadata_cache.revision(raw, archive_key=archive_key, reference=reference, bot_id=bot_id)
        manifest = metadata_cache.get(revision)
        if manifest is not None:
            raw["_archive_manifest"] = manifest
            return raw
    manifest = await _download_manifest(attachment, raw, timeout=timeout, session=session)
    if metadata_cache is not None:
        manifest = validate_manifest(manifest, archive_key=archive_key, reference=reference)
        first = manifest["segments"][0]
        audio = next((item for item in raw.get("attachments") or []
                      if str(item.get("id")) == str(reference["attachment_id"])), None)
        if not audio or first["filename"] != audio.get("filename") or int(first["size_bytes"]) != int(audio.get("size") or 0):
            raise ValueError("primeiro anexo diferente do manifesto")
        metadata_cache.put(revision, manifest)
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
