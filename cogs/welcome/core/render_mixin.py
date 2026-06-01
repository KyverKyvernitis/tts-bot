from __future__ import annotations

import asyncio
import base64
import contextlib
import colorsys
import json
import os
from io import BytesIO
from pathlib import Path
import logging
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord.ext import commands

try:
    from PIL import Image, ImageSequence
except Exception:  # pragma: no cover - fallback if Pillow is unavailable
    Image = None
    ImageSequence = None

from ..config.defaults import *
from .helpers import *

log = logging.getLogger(__name__)

class WelcomeRenderMixin:
    def _member_values(self, member: discord.Member | None, *, guild_id: int | None = None, invite_info: dict[str, Any] | None = None) -> dict[str, str]:
        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None and guild_id:
            guild = self.bot.get_guild(int(guild_id))
        now_ts = int(datetime.now(timezone.utc).timestamp())
        created_at = getattr(member, "created_at", None) if member is not None else None
        created_ts = int(created_at.timestamp()) if created_at else now_ts
        invite = dict(invite_info or {})
        code = str(invite.get("code") or "convite desconhecido")
        inviter_id = int(invite.get("inviter_id") or 0)
        channel_id = int(invite.get("channel_id") or 0)
        inviter_name = str(invite.get("inviter_name") or "quem convidou")
        channel_name = str(invite.get("channel_name") or "canal")
        inviter_member = guild.get_member(inviter_id) if guild is not None and inviter_id else None
        guild_icon = getattr(guild, "icon", None) if guild is not None else None
        bot_user = getattr(self.bot, "user", None)
        member_avatar = str(member.display_avatar.url) if member is not None else ""
        inviter_avatar = str(inviter_member.display_avatar.url) if inviter_member is not None else ""
        server_icon = str(guild_icon.url) if guild_icon else ""
        bot_avatar = str(bot_user.display_avatar.url) if bot_user is not None else ""
        return {
            "membro": str(getattr(member, "display_name", None) or getattr(member, "name", None) or "novo membro"),
            "membro_mencao": str(getattr(member, "mention", None) or "@membro"),
            "usuario": str(getattr(member, "name", None) or getattr(member, "display_name", None) or "membro"),
            "usuario_id": str(getattr(member, "id", "") or ""),
            "membro_id": str(getattr(member, "id", "") or ""),
            "membro_avatar": member_avatar,
            "servidor": str(getattr(guild, "name", None) or "servidor"),
            "servidor_id": str(getattr(guild, "id", "") or guild_id or ""),
            "servidor_icone": server_icon,
            "contador": str(getattr(guild, "member_count", None) or ""),
            "criado_em": f"<t:{created_ts}:D>",
            "criado_relativo": f"<t:{created_ts}:R>",
            "entrou_em": f"<t:{now_ts}:F>",
            "convite_codigo": code,
            "convite": code,
            "convite_canal": channel_name if channel_id else "convite desconhecido",
            "convite_canal_mencao": _channel_mention(channel_id) if channel_id else "convite desconhecido",
            "convite_usos": str(invite.get("uses") or ""),
            "convidador": inviter_name if inviter_id else "convite desconhecido",
            "convidador_nome": inviter_name if inviter_id else "convite desconhecido",
            "convidador_mencao": _user_mention(inviter_id) if inviter_id else "convite desconhecido",
            "convidador_avatar": inviter_avatar,
            "bot_avatar": bot_avatar,
            "convite_desconhecido": "convite desconhecido",
        }

    def _replace_vars(self, text: str, values: dict[str, str]) -> str:
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))
        return VAR_RE.sub(repl, str(text or ""))

    def _build_welcome_text(self, cfg: dict[str, Any], *, member: discord.Member | None, guild_id: int | None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> tuple[str, str, str]:
        values = self._member_values(member, guild_id=guild_id, invite_info=invite_info)
        source = dict(cfg.get("dm") or DEFAULT_DM) if dm else dict(cfg.get("public") or DEFAULT_PUBLIC)
        title = self._replace_vars(str(source.get("title") or ""), values).strip()
        body = self._replace_vars(str(source.get("body") or ""), values).strip()
        footer = self._replace_vars(str(source.get("footer") or ""), values).strip()
        return title, body, footer

    def _is_monochrome_pixels(self, pixels: list[tuple[int, int, int, float, float, float]]) -> bool:
        if not pixels:
            return False
        low_sat = sum(1 for _, _, _, brightness, sat, _ in pixels if sat <= 0.18)
        very_low_sat = sum(1 for _, _, _, brightness, sat, _ in pixels if sat <= 0.10)
        # Se a foto é majoritariamente preto/branco/cinza, isso é o estilo dela.
        # Não force uma cor colorida só porque existe um pequeno ruído saturado no avatar.
        return (low_sat / len(pixels)) >= 0.70 or (very_low_sat / len(pixels)) >= 0.58

    def _monochrome_color_from_pixels(self, pixels: list[tuple[int, int, int, float, float, float]], fallback: str = DEFAULT_ACCENT) -> tuple[int, int, int]:
        if not pixels:
            return self._rgb_from_hex(fallback)
        # Usa apenas pixels pouco saturados quando possível para respeitar imagens P&B.
        mono = [(r, g, b, brightness) for r, g, b, brightness, sat, _ in pixels if sat <= 0.20]
        source = mono or [(r, g, b, brightness) for r, g, b, brightness, _, _ in pixels]
        # Remove só extremos minúsculos de ruído; se preto/branco forem dominantes, ficam.
        source.sort(key=lambda item: item[3])
        cut = max(0, min(len(source) // 12, 24))
        trimmed = source[cut: len(source) - cut] if len(source) > cut * 2 + 4 else source
        if not trimmed:
            trimmed = source
        r = int(sum(px[0] for px in trimmed) / len(trimmed))
        g = int(sum(px[1] for px in trimmed) / len(trimmed))
        b = int(sum(px[2] for px in trimmed) / len(trimmed))
        # Mantém monocromático de verdade, sem puxar para azul/verde por ruído de compressão.
        gray = int(round((r * 0.299 + g * 0.587 + b * 0.114)))
        return (max(0, min(255, gray)), max(0, min(255, gray)), max(0, min(255, gray)))

    def _monochrome_palette_from_rgb(self, rgb: tuple[int, int, int], *, limit: int = 6) -> list[tuple[int, int, int]]:
        base = int(round((rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114)))
        # Pequenas variações de luz/sombra mantendo a família preto/branco/cinza.
        values = [base, min(255, int(base * 1.22 + 10)), max(0, int(base * 0.72)), min(255, int(base * 1.45 + 6)), max(0, int(base * 0.48)), min(255, int(base * 1.08 + 3))]
        palette: list[tuple[int, int, int]] = []
        for value in values:
            value = max(0, min(255, int(value)))
            item = (value, value, value)
            if item not in palette:
                palette.append(item)
            if len(palette) >= limit:
                break
        return palette or [rgb]

    async def _member_avatar_color(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT) -> str:
        if member is None or Image is None:
            return _parse_hex(fallback)
        asset = member.display_avatar.replace(size=64, static_format="png")
        cache_key = str(getattr(asset, "key", None) or asset.url)
        cached = self._avatar_color_cache.get(cache_key)
        if cached:
            return cached
        try:
            data = await asset.read()
            with Image.open(BytesIO(data)) as img:
                img = img.convert("RGBA").resize((32, 32))
                candidates: list[tuple[float, int, int, int]] = []
                fallback_pixels: list[tuple[int, int, int]] = []
                opaque_pixels: list[tuple[int, int, int, float, float, float]] = []
                for r, g, b, a in img.getdata():
                    if a < 90:
                        continue
                    brightness = (r + g + b) / 3
                    h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    opaque_pixels.append((r, g, b, brightness, sat, val))
                    if 8 <= brightness <= 248:
                        fallback_pixels.append((r, g, b))
                    if 28 <= brightness <= 232 and sat >= 0.22 and val >= 0.18:
                        score = sat * 0.72 + val * 0.28
                        candidates.append((score, r, g, b))
                if self._is_monochrome_pixels(opaque_pixels):
                    r, g, b = self._monochrome_color_from_pixels(opaque_pixels, fallback)
                elif candidates:
                    candidates.sort(reverse=True)
                    top = candidates[: max(12, len(candidates) // 6)]
                    r = int(sum(px[1] for px in top) / len(top))
                    g = int(sum(px[2] for px in top) / len(top))
                    b = int(sum(px[3] for px in top) / len(top))
                elif fallback_pixels:
                    r = int(sum(px[0] for px in fallback_pixels) / len(fallback_pixels))
                    g = int(sum(px[1] for px in fallback_pixels) / len(fallback_pixels))
                    b = int(sum(px[2] for px in fallback_pixels) / len(fallback_pixels))
                else:
                    return _parse_hex(fallback)
                result = f"#{r:02X}{g:02X}{b:02X}"
                self._avatar_color_cache[cache_key] = result
                if len(self._avatar_color_cache) > 256:
                    self._avatar_color_cache.pop(next(iter(self._avatar_color_cache)), None)
                return result
        except Exception:
            return _parse_hex(fallback)

    async def _with_dynamic_colors(self, config: dict[str, Any], *, member: discord.Member | None) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if str(cfg.get("accent_color_mode") or "fixed") == "member_avatar":
            cfg["accent_color"] = await self._member_avatar_color(member, cfg.get("accent_color") or DEFAULT_ACCENT)
        embed = self._normalize_embed_config(cfg.get("embed"))
        if str(embed.get("color_mode") or "fixed") == "member_avatar":
            embed["color"] = await self._member_avatar_color(member, embed.get("color") or cfg.get("accent_color") or DEFAULT_ACCENT)
            cfg["embed"] = embed
        return cfg

    def _rgb_from_hex(self, value: Any, fallback: str = DEFAULT_ACCENT) -> tuple[int, int, int]:
        raw = _parse_hex(value, fallback).lstrip("#")
        try:
            return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
        except Exception:
            return (88, 101, 242)

    async def _member_avatar_palette(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT, *, limit: int = 6) -> list[tuple[int, int, int]]:
        if member is None or Image is None:
            return [self._rgb_from_hex(fallback)]
        asset = member.display_avatar.replace(size=128, static_format="png")
        cache_key = str(getattr(asset, "key", None) or asset.url)
        cached = self._avatar_palette_cache.get(cache_key)
        if cached:
            return cached[:limit]
        try:
            data = await asset.read()
            with Image.open(BytesIO(data)) as img:
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                img = img.convert("RGBA").resize((64, 64), resampling)
                buckets: dict[tuple[int, int, int], list[float]] = {}
                fallback_pixels: list[tuple[int, int, int]] = []
                opaque_pixels: list[tuple[int, int, int, float, float, float]] = []
                for r, g, b, a in img.getdata():
                    if a < 90:
                        continue
                    brightness = (r + g + b) / 3
                    h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    opaque_pixels.append((r, g, b, brightness, sat, val))
                    if 8 <= brightness <= 248:
                        fallback_pixels.append((r, g, b))
                    if 28 <= brightness <= 232 and sat >= 0.16 and val >= 0.16:
                        key = (r // 32, g // 32, b // 32)
                        item = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
                        item[0] += 1
                        item[1] += r
                        item[2] += g
                        item[3] += b
                        item[4] += sat * 1.35 + val * 0.35
                if self._is_monochrome_pixels(opaque_pixels):
                    mono_base = self._monochrome_color_from_pixels(opaque_pixels, fallback)
                    palette = self._monochrome_palette_from_rgb(mono_base, limit=limit)
                    self._avatar_palette_cache[cache_key] = palette[:limit]
                    if len(self._avatar_palette_cache) > 128:
                        self._avatar_palette_cache.pop(next(iter(self._avatar_palette_cache)), None)
                    return palette[:limit]
                colors: list[tuple[float, int, int, int]] = []
                for item in buckets.values():
                    count = max(1.0, item[0])
                    r = int(item[1] / count)
                    g = int(item[2] / count)
                    b = int(item[3] / count)
                    score = count * 0.45 + item[4] * 18.0
                    colors.append((score, r, g, b))
                colors.sort(reverse=True)
                palette: list[tuple[int, int, int]] = []
                used_hues: list[float] = []
                for _, r, g, b in colors:
                    hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if any(abs(hue - old) < 0.045 or abs(abs(hue - old) - 1.0) < 0.045 for old in used_hues) and len(palette) >= 3:
                        continue
                    palette.append((r, g, b))
                    used_hues.append(hue)
                    if len(palette) >= limit:
                        break
                if not palette and fallback_pixels:
                    r = int(sum(px[0] for px in fallback_pixels) / len(fallback_pixels))
                    g = int(sum(px[1] for px in fallback_pixels) / len(fallback_pixels))
                    b = int(sum(px[2] for px in fallback_pixels) / len(fallback_pixels))
                    palette = [(r, g, b)]
                if not palette:
                    palette = [self._rgb_from_hex(fallback)]
                while len(palette) < 3:
                    base = palette[-1]
                    h, sat, val = colorsys.rgb_to_hsv(base[0] / 255, base[1] / 255, base[2] / 255)
                    if sat <= 0.10:
                        # Não transforme uma paleta cinza em azul/verde só para preencher slots.
                        palette.extend(self._monochrome_palette_from_rgb(base, limit=max(3, limit))[len(palette):])
                        break
                    h = (h + 0.09 * len(palette)) % 1.0
                    sat = min(1.0, max(0.35, sat + 0.08))
                    val = min(1.0, max(0.45, val + 0.05))
                    rr, gg, bb = colorsys.hsv_to_rgb(h, sat, val)
                    palette.append((int(rr * 255), int(gg * 255), int(bb * 255)))
                self._avatar_palette_cache[cache_key] = palette[:limit]
                if len(self._avatar_palette_cache) > 128:
                    self._avatar_palette_cache.pop(next(iter(self._avatar_palette_cache)), None)
                return palette[:limit]
        except Exception:
            return [self._rgb_from_hex(fallback)]

    def _recolor_star_template(self, palette: list[tuple[int, int, int]]) -> bytes | None:
        if Image is None or not STAR_SEPARATOR_ASSET.exists():
            return None
        try:
            with Image.open(STAR_SEPARATOR_ASSET) as src:
                img = src.convert("RGBA")
            width, height = img.size
            pixels = img.load()
            visited = bytearray(width * height)
            components: list[tuple[float, list[tuple[int, int]]]] = []
            for y in range(height):
                for x in range(width):
                    idx = y * width + x
                    if visited[idx]:
                        continue
                    visited[idx] = 1
                    if pixels[x, y][3] <= 18:
                        continue
                    stack = [(x, y)]
                    points: list[tuple[int, int]] = []
                    sx = 0
                    while stack:
                        px, py = stack.pop()
                        points.append((px, py))
                        sx += px
                        for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                                continue
                            nidx = ny * width + nx
                            if visited[nidx]:
                                continue
                            visited[nidx] = 1
                            if pixels[nx, ny][3] > 18:
                                stack.append((nx, ny))
                    if points:
                        components.append((sx / max(1, len(points)), points))
            components.sort(key=lambda item: item[0])
            palette = palette or [self._rgb_from_hex(DEFAULT_ACCENT)]
            for idx, (_, points) in enumerate(components):
                r, g, b = palette[idx % len(palette)]
                for x, y in points:
                    _, _, _, a = pixels[x, y]
                    pixels[x, y] = (r, g, b, a)
            out = BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
        except Exception as exc:
            log.debug("não consegui recolorir estrelas de boas-vindas: %r", exc)
            return None

    async def _star_separator_file(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT) -> discord.File | None:
        if not STAR_SEPARATOR_ASSET.exists():
            return None
        avatar_key = "default"
        if member is not None:
            asset = member.display_avatar.replace(size=128, static_format="png")
            avatar_key = str(getattr(asset, "key", None) or asset.url)
        cache_key = f"{avatar_key}:{_parse_hex(fallback)}"
        data = self._star_image_cache.get(cache_key)
        if data is None:
            palette = await self._member_avatar_palette(member, fallback)
            data = self._recolor_star_template(palette)
            if data is None:
                try:
                    data = STAR_SEPARATOR_ASSET.read_bytes()
                except Exception:
                    return None
            self._star_image_cache[cache_key] = data
            if len(self._star_image_cache) > 128:
                self._star_image_cache.pop(next(iter(self._star_image_cache)), None)
        return discord.File(BytesIO(data), filename=STAR_SEPARATOR_FILENAME)

    def _drop_dynamic_star_media(self, config: dict[str, Any], *, mode: str) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if mode == "components_v2" and _media_mode(cfg.get("media_mode")) == "avatar_stars":
            cfg["media_mode"] = "none"
            cfg["media_url"] = ""
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed" and str(embed.get("image_mode") or "") == "avatar_stars":
            embed["image_mode"] = "none"
            embed["image_url"] = ""
            cfg["embed"] = embed
        return cfg

    async def _prepare_dynamic_media(self, config: dict[str, Any], *, member: discord.Member | None, mode: str, dm: bool = False) -> tuple[dict[str, Any], list[discord.File]]:
        cfg = self._normalize_config(config)
        if dm:
            return cfg, []
        files: list[discord.File] = []
        needs_stars = False
        if mode == "components_v2" and _media_mode(cfg.get("media_mode")) == "avatar_stars":
            needs_stars = True
            cfg["media_url"] = f"attachment://{STAR_SEPARATOR_FILENAME}"
            cfg["media_mode"] = "custom"
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed" and str(embed.get("image_mode") or "") == "avatar_stars":
            needs_stars = True
            embed["image_mode"] = "custom"
            embed["image_url"] = f"attachment://{STAR_SEPARATOR_FILENAME}"
            cfg["embed"] = embed
        if needs_stars:
            star_file = None
            try:
                star_file = await self._star_separator_file(member, cfg.get("accent_color") or DEFAULT_ACCENT)
            except Exception as exc:
                log.warning("falha ao preparar preset de estrelas de boas-vindas; enviando sem imagem dinâmica: %r", exc)
            if star_file is not None:
                files.append(star_file)
            else:
                # Imagem decorativa nunca pode impedir a mensagem de boas-vindas.
                if mode == "components_v2":
                    cfg["media_url"] = ""
                    cfg["media_mode"] = "none"
                elif mode == "embed":
                    embed = self._normalize_embed_config(cfg.get("embed"))
                    if str(embed.get("image_url") or "").startswith("attachment://"):
                        embed["image_url"] = ""
                        embed["image_mode"] = "none"
                        cfg["embed"] = embed
        return cfg, files

    def _make_welcome_container(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.ui.Container:
        cfg = self._normalize_config(config)
        title, body, footer = self._build_welcome_text(cfg, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        style = str(cfg.get("style") or "complete")
        children: list[discord.ui.Item[Any]] = []
        if title:
            children.append(discord.ui.TextDisplay(_trim(f"# {title}", 900)))
        if body:
            children.append(discord.ui.TextDisplay(_trim(body, 1800 if style != "compact" else 900)))
        media_url = _clean_url(cfg.get("media_url")) if not dm else ""
        if media_url and style == "complete":
            children.extend([discord.ui.Separator(), discord.ui.MediaGallery(discord.MediaGalleryItem(media_url))])
        if footer and style != "compact":
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(_trim(footer, 500))])
        if not children:
            children.append(discord.ui.TextDisplay("# Bem-vindo(a)!"))
        return discord.ui.Container(*children, accent_color=_color_from_hex(cfg.get("accent_color")))

    def _make_components_view(self, config: dict[str, Any], *, member: discord.Member, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(self._make_welcome_container(config, member=member, guild_id=int(member.guild.id), dm=dm, invite_info=invite_info))
        return view

    def _image_url_from_mode(self, mode: str, custom_url: str, *, member: discord.Member | None, guild_id: int | None = None, invite_info: dict[str, Any] | None = None) -> str:
        mode = _image_mode(mode, fallback="none")
        if mode == "none":
            return ""
        if mode == "custom":
            return _clean_url(custom_url)
        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None and guild_id:
            guild = self.bot.get_guild(int(guild_id))
        if mode == "member" and member is not None:
            return str(member.display_avatar.url)
        if mode == "inviter" and guild is not None:
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return str(inviter.display_avatar.url)
        if mode == "server" and guild is not None and getattr(guild, "icon", None):
            return str(guild.icon.url)
        if mode == "bot":
            bot_user = getattr(self.bot, "user", None)
            if bot_user is not None:
                return str(bot_user.display_avatar.url)
        return ""

    def _make_embed_payload(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> tuple[str, discord.Embed]:
        cfg = self._normalize_config(config)
        title, body, footer = self._build_welcome_text(cfg, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        values = self._member_values(member, guild_id=guild_id, invite_info=invite_info)
        embed_cfg = self._normalize_embed_config(cfg.get("embed")) if not dm else dict(DEFAULT_EMBED)
        content = self._replace_vars(str(embed_cfg.get("content") or ""), values).strip() if not dm else ""
        embed_title = self._replace_vars(str(embed_cfg.get("title") or ""), values).strip()
        embed_desc = self._replace_vars(str(embed_cfg.get("description") or body), values).strip()
        embed_footer_source = footer if dm else str(embed_cfg.get("footer_text") or "")
        embed_footer = self._replace_vars(str(embed_footer_source or ""), values).strip()
        embed_color = embed_cfg.get("color") or cfg.get("accent_color")
        embed = discord.Embed(title=_trim(embed_title, 256) or None, description=_trim(embed_desc, 4000) or None, color=_color_from_hex(embed_color))
        title_url = _clean_url(self._replace_vars(str(embed_cfg.get("title_url") or ""), values))
        if title_url:
            embed.url = title_url
        author_name = self._replace_vars(str(embed_cfg.get("author_name") or ""), values).strip()
        if author_name:
            author_icon = self._image_url_from_mode(str(embed_cfg.get("author_icon_mode") or "none"), self._replace_vars(str(embed_cfg.get("author_icon_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
            author_url = _clean_url(self._replace_vars(str(embed_cfg.get("author_url") or ""), values))
            kwargs: dict[str, Any] = {"name": _trim(author_name, 256)}
            if author_icon:
                kwargs["icon_url"] = author_icon
            if author_url:
                kwargs["url"] = author_url
            embed.set_author(**kwargs)
        thumbnail_url = self._image_url_from_mode(str(embed_cfg.get("thumbnail_mode") or "none"), self._replace_vars(str(embed_cfg.get("thumbnail_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        custom_image = self._replace_vars(str(embed_cfg.get("image_url") or cfg.get("media_url") or ""), values)
        image_url = self._image_url_from_mode(str(embed_cfg.get("image_mode") or "custom"), custom_image, member=member, guild_id=guild_id, invite_info=invite_info)
        if image_url:
            embed.set_image(url=image_url)
        if embed_footer:
            footer_icon = self._image_url_from_mode(str(embed_cfg.get("footer_icon_mode") or "none"), self._replace_vars(str(embed_cfg.get("footer_icon_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
            if footer_icon:
                embed.set_footer(text=_trim(embed_footer, 2048), icon_url=footer_icon)
            else:
                embed.set_footer(text=_trim(embed_footer, 2048))
        return _trim(content, 1990), embed

    def _make_embed(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.Embed:
        return self._make_embed_payload(config, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)[1]

    def _make_normal_content(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> str:
        title, body, footer = self._build_welcome_text(config, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        parts = []
        if title:
            parts.append(f"**{title}**")
        if body:
            parts.append(body)
        if dm and footer:
            parts.append(footer)
        return _trim("\n\n".join(parts) or "Bem-vindo(a)!", 1990)

    def _append_render_preview(self, view: discord.ui.LayoutView, config: dict[str, Any], *, member: discord.Member | None, guild_id: int, dm: bool = False):
        cfg = self._normalize_config(config)
        mode = str(cfg.get("dm_render_mode") if dm else cfg.get("render_mode") or "components_v2")
        if mode == "components_v2":
            view.add_item(self._make_welcome_container(cfg, member=member, guild_id=guild_id, dm=dm))
            return
        if mode == "normal":
            content = self._make_normal_content(cfg, member=member, guild_id=guild_id, dm=dm)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(_trim("## Mensagem normal\n" + content)), accent_color=_color_from_hex(cfg.get("accent_color"))))
            return
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Embed\nO preview real em embed é enviado como uma mensagem separada, para aparecer igual ao Discord mostra."),
            accent_color=_color_from_hex((cfg.get("embed") or {}).get("color") or cfg.get("accent_color")),
        ))
