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
        resampling = getattr(Image, 'Resampling', Image)
        square = ImageOps.fit(source.convert("RGB"), (128, 128), method=resampling.LANCZOS)
        out = io.BytesIO()
        square.save(out, format="JPEG", quality=82, optimize=True)
    result = out.getvalue()
    if len(result) >= 256 * 1024:
        raise ValueError("emoji_too_large")
    return result


class ApplicationSelectEmojis:
    """Reuse registered emojis and retry missing assets independently."""

    def __init__(self, namespace, assets, ids, *, dynamic_ids=None, remote_assets=None,
                 version_for_id=None):
        self.namespace, self.assets, self.ids = namespace, assets, ids
        self.dynamic_ids, self.remote_assets = dynamic_ids, remote_assets
        self.version_for_id = version_for_id
        self._known = {}
        self.generation = 0
        self._bot = None
        self._task = None
        self._retry_handle = None
        self._closed = False
        self._blocked_until = float('-inf')
        self._next_sync_at = float('-inf')
        self._fetched_at = float('-inf')
        self._fetch_failures = 0
        self._views = weakref.WeakSet()
        self._requested_ids = ()
        self._pending = {}

    def _asset_ids(self, bot):
        base = list(self.dynamic_ids(bot)) if self.dynamic_ids is not None else []
        base.extend(self.ids())
        return tuple(dict.fromkeys(base))

    def _name(self, asset_id):
        version = self.version_for_id(asset_id) if self.version_for_id else ''
        return emoji_name(self.namespace, f'{asset_id}:{version}' if version else asset_id)

    def emoji(self, asset_id):
        if not isinstance(asset_id, str) or not asset_id:
            return None
        name = self._name(asset_id)
        identity = self._known.get(name)
        if identity is None:
            return None
        import discord
        return discord.PartialEmoji(name=name, id=identity)

    def diagnostics(self, asset_id):
        name = self._name(asset_id)
        if name in self._known:
            return {'asset': asset_id, 'state': 'ready', 'emoji_id': self._known[name]}
        return {'asset': asset_id, **self._pending.get(name, {'state': 'not_checked'})}

    def close(self):
        self._closed = True
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @staticmethod
    def _error_fields(error):
        return {'error_type': type(error).__name__, 'http_status': getattr(error, 'status', 0),
                'discord_code': getattr(error, 'code', 0)}

    @staticmethod
    def _retry_delay(error, attempt):
        if getattr(error, 'status', 0) == 403 or getattr(error, 'code', 0) in (30008, 30018):
            return 900
        try:
            value = float(getattr(error, 'retry_after', 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return min(3600, max(1, value)) if value > 0 else min(900, 30 * 2 ** min(attempt - 1, 5))

    def _failed(self, asset_id, name, state, error=None):
        previous = self._pending.get(name, {})
        attempt = previous.get('attempt', 0) + 1 if previous.get('state') == state else 1
        delay = self._retry_delay(error, attempt) if error else 60
        fields = self._error_fields(error) if error else {}
        self._pending[name] = {'state': state, 'attempt': attempt,
                               'retry_at': time.monotonic() + delay, **fields}
        if error:
            log.warning('[cd_emoji] asset=%s state=%s type=%s status=%s code=%s retry_s=%s',
                        asset_id, state, fields['error_type'], fields['http_status'],
                        fields['discord_code'], delay)
        elif previous.get('state') != state:
            version = self.version_for_id(asset_id) if self.version_for_id else ''
            report = log.warning if version else log.info
            report('[cd_emoji] asset=%s state=%s', asset_id, state)

    def _arm_retry(self):
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
        if self._closed or not self._pending or self._bot is None:
            return
        checker = getattr(self._bot, 'is_closed', None)
        if callable(checker) and checker():
            return
        deadline = max(self._next_sync_at, self._blocked_until,
                       min(item['retry_at'] for item in self._pending.values()))
        self._retry_handle = asyncio.get_running_loop().call_later(
            max(0.1, deadline - time.monotonic()), self.schedule, self._bot)

    def schedule(self, bot, view=None):
        if (bot is None or not callable(getattr(bot, 'fetch_application_emojis', None))
                or not callable(getattr(bot, 'create_application_emoji', None))):
            return
        if view is not None:
            try:
                self._views.add(view)
            except TypeError:
                pass
        if self._bot is not bot:
            self.close()
            self._task = None
            self._bot = bot
            self._known.clear()
            self._pending.clear()
            self._fetched_at = self._next_sync_at = float('-inf')
            self._blocked_until = float('-inf')
        self._closed = False
        requested = tuple(self._name(asset) for asset in self._asset_ids(bot))
        if requested != self._requested_ids:
            self._requested_ids = requested
            self._next_sync_at = float('-inf')
        requested_names = set(requested)
        self._pending = {name: row for name, row in self._pending.items()
                         if name in requested_names and name not in self._known}
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        if now < max(self._next_sync_at, self._blocked_until):
            self._arm_retry()
            return
        due = any(name not in self._known and self._pending.get(name, {}).get('retry_at', 0) <= now
                  for name in requested)
        if not due and now - self._fetched_at < 86400:
            self._arm_retry()
            return
        self._task = asyncio.create_task(self._synchronize(bot))

    async def _refresh_views(self):
        for view in tuple(self._views):
            if view._is_expired() or getattr(view, 'is_finished', lambda: False)():
                continue
            try:
                if getattr(view, 'message', None) is not None:
                    await view._edit_panel()
                else:
                    view._rebuild()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.debug('[cd_emoji] panel_update type=%s', type(error).__name__)

    async def _synchronize(self, bot):
        requested = self._requested_ids
        previous = dict(self._known)
        try:
            now = time.monotonic()
            if now - self._fetched_at >= 86400:
                try:
                    remote = await bot.fetch_application_emojis()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._fetch_failures += 1
                    delay = self._retry_delay(error, self._fetch_failures)
                    self._next_sync_at = now + delay
                    for asset in self._asset_ids(bot):
                        name = self._name(asset)
                        if name not in self._known:
                            self._failed(asset, name, 'fetch_failed', error)
                    return
                if bot is not self._bot:
                    return
                self._known = {entry.name: entry.id for entry in remote
                               if isinstance(getattr(entry, 'name', None), str)
                               and type(getattr(entry, 'id', None)) is int}
                self._fetched_at, self._fetch_failures = time.monotonic(), 0
            for asset_id in self._asset_ids(bot):
                name = self._name(asset_id)
                if name in self._known:
                    self._pending.pop(name, None)
                    continue
                if self._pending.get(name, {}).get('retry_at', 0) > time.monotonic():
                    continue
                try:
                    cover = self.assets(asset_id)
                    remote_asset = False
                    if not cover and self.remote_assets is not None:
                        cover = self.remote_assets(bot, asset_id)
                        if inspect.isawaitable(cover):
                            cover = await cover
                        remote_asset = bool(cover)
                    if not cover:
                        self._failed(asset_id, name, 'local_asset_missing')
                        continue
                    image = square_emoji(cover, allow_avatar_formats=remote_asset)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._failed(asset_id, name, 'image_invalid', error)
                    continue
                if bot is not self._bot or name != self._name(asset_id):
                    continue
                try:
                    created = await bot.create_application_emoji(name=name, image=image)
                    if bot is not self._bot or self._closed:
                        return
                    self._known[name] = int(created.id)
                    self._pending.pop(name, None)
                    log.info('[cd_emoji] asset=%s state=created emoji_id=%s', asset_id, created.id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._failed(asset_id, name, 'creation_failed', error)
                    # Refresh the remote list before retrying an uncertain POST;
                    # a timeout may have created the emoji on Discord already.
                    self._fetched_at = float('-inf')
                    if getattr(error, 'status', 0) == 403 or getattr(error, 'code', 0) in (30008, 30018):
                        self._blocked_until = time.monotonic() + 900
                        break
                    if getattr(error, 'status', 0) == 429:
                        self._blocked_until = self._pending[name]['retry_at']
                        break
                    continue
        finally:
            if bot is self._bot and self._task is asyncio.current_task():
                if not self._closed and self._known != previous:
                    self.generation += 1
                    await self._refresh_views()
                self._task = None
                if not self._closed and requested != self._requested_ids:
                    self._next_sync_at = float('-inf')
                    self.schedule(bot)
                elif not self._closed:
                    self._arm_retry()
