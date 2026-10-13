"""Reusable, lazy registry of Discord *application* emojis for select menus.

One emoji per asset is created for the application, never per guild or user.
Rendering a menu is network-free: missing emojis simply omit the option emoji.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import inspect
import logging
import re
import time
import weakref
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)
_VALID = re.compile(r"[^a-z0-9_]+")


def emoji_name(namespace: str, asset_id: str) -> str:
    """Stable, collision-resistant Discord emoji name (2..32 ASCII chars)."""
    slug = _VALID.sub("_", asset_id.lower().replace("-", "_")).strip("_") or "item"
    prefix = _VALID.sub("_", namespace.lower()).strip("_")[:8] or "app"
    digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:6]
    return f"{prefix}_{slug[:32-len(prefix)-8]}_{digest}"


def square_emoji(image: bytes, *, allow_avatar_formats: bool = False) -> bytes:
    """Fit local, prevalidated thumbnail to a small application emoji JPEG."""
    from PIL import Image, ImageOps
    if len(image) > 2 * 1024 * 1024:
        raise ValueError("image_too_large")
    with Image.open(io.BytesIO(image)) as source:
        formats = {"JPEG", "PNG", "WEBP", "GIF"} if allow_avatar_formats else {"JPEG"}
        max_dimension = 2048 if allow_avatar_formats else 768
        if source.format not in formats or not (0 < source.width <= max_dimension and 0 < source.height <= max_dimension):
            raise ValueError("invalid_cover")
        source.load()
        square = ImageOps.fit(source.convert("RGB"), (128, 128), method=Image.Resampling.LANCZOS)
        out = io.BytesIO()
        square.save(out, format="JPEG", quality=82, optimize=True)
    result = out.getvalue()
    if len(result) >= 256 * 1024:
        raise ValueError("emoji_too_large")
    return result


class ApplicationSelectEmojis:
    """Singleflight sync. Existing app emojis are reused after every restart."""

    def __init__(self, namespace: str, assets: Callable[[str], bytes | None], ids: Callable[[], Iterable[str]],
                 *, dynamic_ids: Callable[[object], Iterable[str]] | None = None,
                 remote_assets: Callable[[object, str], object] | None = None,
                 version_for_id: Callable[[str], str] | None = None):
        self.namespace = namespace
        self.assets = assets
        self.ids = ids
        self.dynamic_ids = dynamic_ids
        self.remote_assets = remote_assets
        self.version_for_id = version_for_id
        self._known: dict[str, int] = {}
        self._bot = None
        self._task: asyncio.Task | None = None
        self._next_sync_at = float("-inf")
        self._views = weakref.WeakSet()
        self._requested_ids: tuple[str, ...] = ()

    def _asset_ids(self, bot) -> tuple[str, ...]:
        base = list(self.dynamic_ids(bot)) if self.dynamic_ids is not None else []
        base.extend(self.ids())
        return tuple(dict.fromkeys(base))

    def _name(self, asset_id: str) -> str:
        version = self.version_for_id(asset_id) if self.version_for_id else ""
        return emoji_name(self.namespace, f"{asset_id}:{version}" if version else asset_id)

    def emoji(self, asset_id: str | None):
        if not isinstance(asset_id, str) or not asset_id:
            return None
        name = self._name(asset_id)
        emoji_id = self._known.get(name)
        if not emoji_id:
            return None
        # import locally so the module can be tested without a Discord connection
        import discord
        return discord.PartialEmoji(name=name, id=emoji_id)

    def schedule(self, bot, view=None) -> None:
        if (bot is None or not callable(getattr(bot, "fetch_application_emojis", None))
                or not callable(getattr(bot, "create_application_emoji", None))):
            return
        if view is not None:
            try:
                self._views.add(view)
            except TypeError:
                pass
        if self._bot is not bot:
            self._bot = bot
            self._known.clear()
            self._requested_ids = ()
            self._next_sync_at = float("-inf")
        requested = tuple(self._name(asset_id) for asset_id in self._asset_ids(bot))
        if requested != self._requested_ids:
            self._requested_ids = requested
            self._next_sync_at = float("-inf")
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        # Do not hammer Discord on failures or when no local images exist.
        if now < self._next_sync_at:
            return
        self._next_sync_at = now + 300
        self._task = asyncio.create_task(self._synchronize(bot))

    async def _synchronize(self, bot) -> None:
        requested = self._requested_ids
        try:
            remote = await bot.fetch_application_emojis()
            if bot is not self._bot:
                return
            known = {str(e.name): int(e.id) for e in remote
                     if isinstance(getattr(e, "name", None), str) and type(getattr(e, "id", None)) is int}
            changed = False
            asset_ids = self._asset_ids(bot)
            for asset_id in asset_ids:
                name = self._name(asset_id)
                if name in known:
                    continue
                cover = self.assets(asset_id)
                is_remote_asset = False
                if not cover and self.remote_assets is not None:
                    try:
                        cover = self.remote_assets(bot, asset_id)
                        if inspect.isawaitable(cover):
                            cover = await cover
                        is_remote_asset = bool(cover)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug("[cd_emoji] imagem remota indisponível | type=%s", type(exc).__name__)
                        continue
                if not cover:
                    continue
                # A thumbnail can be replaced while Discord metadata is fetched.
                # Bind its generated emoji to the name of the bytes actually read.
                if name != self._name(asset_id):
                    continue
                try:
                    created = await bot.create_application_emoji(
                        name=name, image=square_emoji(cover, allow_avatar_formats=is_remote_asset)
                    )
                    known[name] = int(created.id)
                    changed = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep the panel usable even with quota exhaustion or no API permission.
                    log.warning("[cd_emoji] criação indisponível | type=%s", type(exc).__name__)
                    break
            previous = self._known
            self._known = known
            if all(self._name(asset_id) in known for asset_id in asset_ids):
                # All covers registered; do not poll the API every time a user opens CD.
                self._next_sync_at = time.monotonic() + 24 * 3600
            if changed or known != previous:
                for view in tuple(self._views):
                    if view._is_expired():
                        continue
                    try:
                        view._rebuild()
                        if getattr(view, "message", None) is not None:
                            await view._edit_panel()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug("[cd_emoji] painel não atualizado | type=%s", type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[cd_emoji] consulta indisponível | type=%s", type(exc).__name__)
        finally:
            if bot is self._bot and requested != self._requested_ids:
                # Another cover revision arrived during this singleflight task.
                # It should not inherit the failed/old revision's five-minute wait.
                self._task = None
                self._next_sync_at = float("-inf")
                self.schedule(bot)
