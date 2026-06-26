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
import secrets
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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

class WelcomeMediaMixin:
    def _emoji_tokens_from_config(self, cfg: dict[str, Any], *, mode: str, dm: bool = False, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT) -> list[dict[str, Any]]:
        """Detecta emojis customizados usados na mensagem.

        O limite vale para emojis base diferentes, não para quantas vezes o mesmo emoji
        aparece. Se o mesmo emoji aparece em várias linhas, criamos um emoji temporário
        só e substituímos todas as aparições conhecidas dele. Também agrupamos pelo ID
        do emoji, porque o mesmo emoji pode aparecer com nomes diferentes no texto salvo.
        """
        if dm:
            return []
        try:
            effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
        except Exception:
            effective_limit = DEFAULT_DECORATIVE_EMOJI_LIMIT
        if effective_limit <= 0:
            return []
        mode = str(mode or cfg.get("render_mode") or "components_v2")
        texts: list[str] = []
        public = dict(cfg.get("public") or {})
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed":
            for key in ("content", "author_name", "title", "description", "footer_text"):
                texts.append(str(embed.get(key) or ""))
            # Se a descrição do embed estiver vazia, o corpo público vira fallback.
            if not str(embed.get("description") or ""):
                texts.extend(str(public.get(key) or "") for key in ("title", "body", "footer"))
        else:
            texts.extend(str(public.get(key) or "") for key in ("title", "body", "footer"))

        found: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for text in texts:
            for match in CUSTOM_EMOJI_RE.finditer(str(text or "")):
                raw = match.group(0)
                emoji_id = str(match.group(3) or "")
                key = f"{'a' if bool(match.group(1)) else 's'}:{emoji_id}"
                item = by_key.get(key)
                if item is not None:
                    item["occurrences"] = int(item.get("occurrences") or 1) + 1
                    raws = item.setdefault("raw_variants", [])
                    if raw not in raws:
                        raws.append(raw)
                    continue
                if len(found) >= effective_limit:
                    # O restante fica original. Nunca removemos nem trocamos por texto vazio.
                    continue
                item = {
                    "raw": raw,
                    "raw_variants": [raw],
                    "key": key,
                    "animated": bool(match.group(1)),
                    "name": str(match.group(2) or "emoji")[:32],
                    "id": emoji_id,
                    "occurrences": 1,
                }
                by_key[key] = item
                found.append(item)
        return found

    def _replace_emoji_tokens_in_config(self, cfg: dict[str, Any], replacements: dict[str, str], *, mode: str, dm: bool = False) -> dict[str, Any]:
        """Substitui emojis decorativos sem corromper fallback.

        `replacements` aceita duas formas:
        - chave raw (`<:nome:id>` / `<a:nome:id>`) para compatibilidade;
        - chave `id:<emoji_id>` para trocar globalmente qualquer ocorrência daquele ID,
          mesmo que o nome salvo no texto seja diferente.

        Se um emoji não tiver replacement confirmado, o texto fica exatamente como estava.
        """
        if not replacements or dm:
            return cfg
        out = self._normalize_config(cfg)
        id_replacements: dict[str, str] = {}
        raw_replacements: dict[str, str] = {}
        for old, new in (replacements or {}).items():
            old_s = str(old or "")
            new_s = str(new or "")
            if not old_s or not new_s:
                continue
            if old_s.startswith("id:"):
                emoji_id = old_s[3:]
                if re.fullmatch(r"\d{15,25}", emoji_id):
                    id_replacements[emoji_id] = new_s
            else:
                raw_replacements[old_s] = new_s

        def repl(text: Any) -> str:
            value = str(text or "")
            if id_replacements:
                def by_id(match: re.Match[str]) -> str:
                    emoji_id = str(match.group(3) or "")
                    return id_replacements.get(emoji_id, match.group(0))
                value = CUSTOM_EMOJI_RE.sub(by_id, value)
            # Raw fallback para qualquer variação não coberta por ID.
            for old, new in raw_replacements.items():
                value = value.replace(old, new)
            return value

        public = dict(out.get("public") or {})
        for key in ("title", "body", "footer"):
            public[key] = repl(public.get(key))
        out["public"] = public
        embed = self._normalize_embed_config(out.get("embed"))
        for key in ("content", "author_name", "title", "description", "footer_text"):
            embed[key] = repl(embed.get(key))
        out["embed"] = embed
        return out

    def _emoji_cdn_url(self, emoji: dict[str, Any]) -> str:
        ext = "gif" if bool(emoji.get("animated")) else "png"
        return f"https://cdn.discordapp.com/emojis/{emoji.get('id')}.{ext}?size=128&quality=lossless"

    def _fetch_url_bytes_sync(self, url: str, *, timeout: float = 4.0, limit: int = 900_000) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "CoreWelcomeBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("asset grande demais")
        return data

    async def _fetch_custom_emoji_bytes(self, emoji: dict[str, Any]) -> bytes:
        return await asyncio.to_thread(self._fetch_url_bytes_sync, self._emoji_cdn_url(emoji), timeout=4.0, limit=900_000)

    def _mix_rgb(self, first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        amount = max(0.0, min(1.0, float(amount)))
        return (
            max(0, min(255, int(round(first[0] * (1.0 - amount) + second[0] * amount)))),
            max(0, min(255, int(round(first[1] * (1.0 - amount) + second[1] * amount)))),
            max(0, min(255, int(round(first[2] * (1.0 - amount) + second[2] * amount)))),
        )

    def _adjust_rgb_hsv(self, rgb: tuple[int, int, int], *, sat_mul: float = 1.0, val_mul: float = 1.0, hue_shift: float = 0.0) -> tuple[int, int, int]:
        h, sat, val = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        h = (h + hue_shift) % 1.0
        sat = max(0.0, min(1.0, sat * sat_mul))
        val = max(0.0, min(1.0, val * val_mul))
        r, g, b = colorsys.hsv_to_rgb(h, sat, val)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _subtle_emoji_palette(self, base_rgb: tuple[int, int, int], avatar_palette: list[tuple[int, int, int]] | None = None) -> list[tuple[int, int, int]]:
        """Cria uma paleta coerente com a cor final da mensagem.

        A cor principal do emoji é sempre a cor efetiva do embed/visual. As cores do
        avatar entram só como nuances discretas, para não virar arco-íris nem fugir do
        tema escolhido pelo servidor.
        """
        palette: list[tuple[int, int, int]] = [base_rgb]
        palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=0.92, val_mul=1.24))
        palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=1.06, val_mul=0.68))
        base_h, base_s, base_v = colorsys.rgb_to_hsv(base_rgb[0] / 255, base_rgb[1] / 255, base_rgb[2] / 255)
        for raw in avatar_palette or []:
            try:
                ah, asat, aval = colorsys.rgb_to_hsv(raw[0] / 255, raw[1] / 255, raw[2] / 255)
            except Exception:
                continue
            # Mantém a variação perto da cor principal. Mesmo se o avatar tiver uma cor
            # muito diferente, usamos só uma influência pequena.
            diff = ((ah - base_h + 0.5) % 1.0) - 0.5
            hue_shift = max(-0.035, min(0.035, diff * 0.18))
            sat_mul = 0.96 + max(-0.10, min(0.10, (asat - base_s) * 0.18))
            val_mul = 0.96 + max(-0.12, min(0.12, (aval - base_v) * 0.22))
            candidate = self._adjust_rgb_hsv(base_rgb, sat_mul=sat_mul, val_mul=val_mul, hue_shift=hue_shift)
            if candidate not in palette:
                palette.append(candidate)
            if len(palette) >= 6:
                break
        while len(palette) < 4:
            shift = 0.018 * len(palette)
            palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=1.0, val_mul=1.0 + (0.06 if len(palette) % 2 else -0.06), hue_shift=shift))
        return palette[:6]

    def _palette_is_mostly_monochrome(self, palette: list[tuple[int, int, int]] | None) -> bool:
        values = list(palette or [])[:6]
        if not values:
            return False
        low_sat = 0
        for r, g, b in values:
            try:
                _h, sat, _val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            except Exception:
                continue
            if sat < 0.14:
                low_sat += 1
        return low_sat >= max(1, len(values) - 1)

    def _hex_palette_from_rgb(self, palette: list[tuple[int, int, int]]) -> list[str]:
        return [f"#{r:02X}{g:02X}{b:02X}" for r, g, b in (palette or [])]

    def _palette_from_hex_list(self, values: Any, fallback: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        if isinstance(values, list):
            for item in values[:8]:
                try:
                    result.append(self._rgb_from_hex(item, f"#{fallback[0]:02X}{fallback[1]:02X}{fallback[2]:02X}"))
                except Exception:
                    continue
        return result or [fallback]

    def _fit_emoji_canvas_frame(self, frame: Any, *, canvas_size: int = 128) -> Any:
        """Ajusta o canvas inteiro para 128x128 preservando padding/posição.

        Não recorta a área visível. Isso é importante porque alguns emojis têm espaço
        transparente proposital; o recolorido deve manter o tamanho visual original e
        não crescer nem virar pontinho por causa de geometria diferente.
        """
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        rgba = frame.convert("RGBA")
        if rgba.size == (canvas_size, canvas_size):
            return rgba.copy()
        width, height = rgba.size
        if width <= 0 or height <= 0:
            return Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        scale = min(canvas_size / float(width), canvas_size / float(height))
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        resized = rgba.resize(new_size, resampling)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        canvas.alpha_composite(resized, ((canvas_size - resized.width) // 2, (canvas_size - resized.height) // 2))
        return canvas

    def _recolor_rgba_image(self, img: Any, rgb: tuple[int, int, int], palette: list[tuple[int, int, int]] | None = None) -> Any:
        img = img.convert("RGBA")
        px = img.load()
        base = rgb
        usable_palette = palette or [base]
        light = usable_palette[1] if len(usable_palette) > 1 else self._adjust_rgb_hsv(base, sat_mul=0.92, val_mul=1.22)
        dark = usable_palette[2] if len(usable_palette) > 2 else self._adjust_rgb_hsv(base, sat_mul=1.04, val_mul=0.68)
        accents = usable_palette[3:] or [base]
        width, height = img.size
        for y in range(height):
            for x in range(width):
                r, g, b, a = px[x, y]
                if a < 8:
                    continue
                lum = max(0.0, min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0))
                if lum < 0.50:
                    target = self._mix_rgb(dark, base, lum / 0.50)
                else:
                    target = self._mix_rgb(base, light, (lum - 0.50) / 0.50)
                # Pequena nuance da paleta perto da cor principal. A influência é baixa
                # para preservar um tema único baseado na cor do embed.
                try:
                    oh, osat, oval = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if accents and osat > 0.08:
                        accent = accents[(int(oh * 12) + (x // 24) + (y // 24)) % len(accents)]
                        target = self._mix_rgb(target, accent, 0.10)
                except Exception:
                    pass
                px[x, y] = (target[0], target[1], target[2], a)
        return img

    def _save_static_emoji_png(self, img: Any) -> bytes:
        candidate = self._fit_emoji_canvas_frame(img, canvas_size=128)
        out = BytesIO()
        candidate.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= DISCORD_EMOJI_MAX_BYTES:
            return data
        # PNG de 128x128 normalmente fica muito abaixo disso; se não ficar, reduz cores
        # sem mexer na geometria visual.
        quantized = candidate.convert("P", palette=Image.Palette.ADAPTIVE, colors=96).convert("RGBA") if Image is not None else candidate
        out = BytesIO()
        quantized.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= DISCORD_EMOJI_MAX_BYTES:
            return data
        raise RuntimeError("emoji estático ficou maior que 256 KiB")

    def _save_animated_emoji_gif(self, frames: list[Any], durations: list[int]) -> bytes | None:
        if not frames:
            return None
        normalized_frames = [self._fit_emoji_canvas_frame(frame, canvas_size=128) for frame in frames]
        for step in (1, 2, 3, 4, 5, 6, 8, 10):
            selected = [frame for idx, frame in enumerate(normalized_frames) if idx % step == 0]
            selected_durations = [max(20, min(500, int((durations[idx] if idx < len(durations) else 80) * step))) for idx in range(len(normalized_frames)) if idx % step == 0]
            if not selected:
                continue
            out = BytesIO()
            selected[0].save(
                out,
                format="GIF",
                save_all=True,
                append_images=selected[1:],
                duration=selected_durations,
                loop=0,
                optimize=True,
                disposal=2,
            )
            data = out.getvalue()
            if len(data) <= DISCORD_EMOJI_MAX_BYTES:
                return data
        return None

    def _normalize_emoji_upload_bytes_sync(self, raw: bytes, *, animated: bool) -> tuple[bytes, str]:
        """Garante formato aceito pelo Discord preservando o tamanho visual original."""
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        with Image.open(BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                frames = [self._fit_emoji_canvas_frame(frame) for frame in raw_frames]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            return self._save_static_emoji_png(img.convert("RGBA")), "png"

    async def _normalize_emoji_upload_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw_b64 = str(item.get("data_b64") or "")
            if not raw_b64:
                return None
            raw = base64.b64decode(raw_b64)
            animated = str(item.get("format") or "").lower() == "gif" or bool(item.get("animated"))
            data, fmt = await asyncio.to_thread(self._normalize_emoji_upload_bytes_sync, raw, animated=animated)
            return {**item, "data_b64": base64.b64encode(data).decode("ascii"), "format": fmt, "animated": fmt == "gif", "size": len(data)}
        except Exception as exc:
            log.debug("não consegui normalizar emoji temporário antes do upload: %r", exc)
            return None

    def _recolor_emoji_bytes_local_sync(self, raw: bytes, *, animated: bool, color_hex: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        base_rgb = self._rgb_from_hex(color_hex)
        subtle_palette = palette or self._subtle_emoji_palette(base_rgb, [])
        with Image.open(BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                frames = [self._recolor_rgba_image(self._fit_emoji_canvas_frame(frame), base_rgb, subtle_palette) for frame in raw_frames]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            fitted = self._fit_emoji_canvas_frame(img.convert("RGBA"))
            out_img = self._recolor_rgba_image(fitted, base_rgb, subtle_palette)
            return self._save_static_emoji_png(out_img), "png"

    async def _recolor_emoji_bytes_local(self, raw: bytes, *, animated: bool, color_hex: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._recolor_emoji_bytes_local_sync, raw, animated=animated, color_hex=color_hex, palette=palette)

    def _phone_worker_base_url(self) -> str:
        enabled = str(os.getenv("PHONE_WORKER_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on", "sim"}
        host = str(os.getenv("PHONE_WORKER_HOST") or "").strip()
        if not enabled or not host:
            return ""
        scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
        try:
            port = int(str(os.getenv("PHONE_WORKER_PORT") or "8766"))
        except Exception:
            port = 8766
        return f"{scheme}://{host}:{port}"

    async def _worker_recolor_emojis(self, emojis: list[dict[str, Any]], *, color_hex: str, palette_hex: list[str] | None = None, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT, monochrome: bool = False) -> list[dict[str, Any]] | None:
        base_url = self._phone_worker_base_url()
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        if not base_url or not token or not emojis:
            return None
        worker_key = base_url
        if int(self._emoji_worker_active.get(worker_key, 0) or 0) >= 2:
            return None
        self._emoji_worker_active[worker_key] = int(self._emoji_worker_active.get(worker_key, 0) or 0) + 1
        try:
            effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
            payload = json.dumps({"task": "emoji_recolor", "color": color_hex, "palette": palette_hex or [], "monochrome": bool(monochrome), "emojis": emojis[:effective_limit]}).encode("utf-8")
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            def post() -> dict[str, Any]:
                req = urllib.request.Request(f"{base_url}/task", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=7.0) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            data = await asyncio.to_thread(post)
            if not isinstance(data, dict) or data.get("ok") is False:
                return None
            items = data.get("items") if isinstance(data.get("items"), list) else []
            return [item for item in items if isinstance(item, dict)]
        except Exception as exc:
            log.debug("worker turbo não recoloriu emojis de boas-vindas: %r", exc)
            return None
        finally:
            self._emoji_worker_active[worker_key] = max(0, int(self._emoji_worker_active.get(worker_key, 1) or 1) - 1)

    async def _local_recolor_emojis(self, emojis: list[dict[str, Any]], *, color_hex: str, palette: list[tuple[int, int, int]] | None = None, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
        for emoji in emojis[:effective_limit]:
            try:
                raw = await self._fetch_custom_emoji_bytes(emoji)
                data, fmt = await self._recolor_emoji_bytes_local(raw, animated=bool(emoji.get("animated")), color_hex=color_hex, palette=palette)
                result.append({**emoji, "data_b64": base64.b64encode(data).decode("ascii"), "format": fmt})
            except Exception as exc:
                log.debug("não consegui recolorir emoji localmente: %s %r", emoji.get("raw"), exc)
        return result

    async def _application_id(self) -> int:
        app_id = int(getattr(self.bot, "application_id", 0) or 0)
        if app_id:
            return app_id
        info = await self.bot.application_info()
        return int(info.id)

    def _next_welcome_midnight_utc(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        try:
            local_tz = ZoneInfo(WELCOME_EMOJI_TIMEZONE)
        except Exception:
            local_tz = timezone(timedelta(hours=-3))
        local_now = current.astimezone(local_tz)
        local_midnight = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(timezone.utc)

    async def _refresh_application_emoji_state_locked(self, *, force: bool = False) -> list[dict[str, Any]]:
        cached_at = float(getattr(self, "_application_emoji_state_at", 0.0) or 0.0)
        cached_items = getattr(self, "_application_emoji_items", None)
        if not force and isinstance(cached_items, list) and (time.monotonic() - cached_at) < 15.0:
            return list(cached_items)

        app_id = await self._application_id()
        from discord.http import Route
        request = getattr(getattr(self.bot, "http", None), "request", None)
        if not app_id or not callable(request):
            return []
        data = await request(Route("GET", "/applications/{application_id}/emojis", application_id=app_id))
        raw_items = data.get("items") if isinstance(data, dict) else data
        items = [dict(item) for item in (raw_items or []) if isinstance(item, dict) and item.get("id")]
        self._application_emoji_items = items
        self._application_emoji_names = {str(item.get("name") or "") for item in items if str(item.get("name") or "")}
        self._application_emoji_count = len(items)
        self._application_emoji_state_at = time.monotonic()
        return list(items)

    async def _application_emojis_snapshot(self, *, force: bool = False) -> list[dict[str, Any]]:
        lock = getattr(self, "_emoji_api_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._emoji_api_lock = lock
        async with lock:
            return await self._refresh_application_emoji_state_locked(force=force)

    def _numeric_application_emoji_name_locked(self) -> str:
        existing = set(getattr(self, "_application_emoji_names", set()) or set())
        reserved = set(getattr(self, "_emoji_name_reservations", set()) or set())
        for _ in range(200):
            candidate = f"{secrets.randbelow(1_000_000):06d}"
            if candidate not in existing and candidate not in reserved:
                reserved.add(candidate)
                self._emoji_name_reservations = reserved
                return candidate
        raise RuntimeError("não consegui reservar nome numérico para application emoji")

    async def _create_application_emoji(self, *, name: str, data_b64: str, fmt: str) -> dict[str, Any] | None:
        del name  # O nome público é sempre um número aleatório de seis dígitos.
        lock = getattr(self, "_emoji_api_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._emoji_api_lock = lock
        reserved_name = ""
        try:
            app_id = await self._application_id()
            if not app_id:
                return None
            fmt = "gif" if str(fmt or "").lower() == "gif" else "png"
            image_data = f"data:image/{fmt};base64,{data_b64}"
            from discord.http import Route
            request = getattr(getattr(self.bot, "http", None), "request", None)
            if not callable(request):
                return None

            async with lock:
                await self._refresh_application_emoji_state_locked()
                current_count = int(getattr(self, "_application_emoji_count", 0) or 0)
                usable_limit = max(0, WELCOME_APPLICATION_EMOJI_LIMIT - WELCOME_APPLICATION_EMOJI_RESERVED_SLOTS)
                if current_count >= usable_limit:
                    last_warning = float(getattr(self, "_emoji_capacity_warning_at", 0.0) or 0.0)
                    if (time.monotonic() - last_warning) >= 60.0:
                        log.warning(
                            "application emojis sem margem para boas-vindas: total=%s limite_operacional=%s; usando emojis originais",
                            current_count,
                            usable_limit,
                        )
                        self._emoji_capacity_warning_at = time.monotonic()
                    return None

                reserved_name = self._numeric_application_emoji_name_locked()
                try:
                    data = await request(
                        Route("POST", "/applications/{application_id}/emojis", application_id=app_id),
                        json={"name": reserved_name, "image": image_data},
                    )
                finally:
                    reservations = set(getattr(self, "_emoji_name_reservations", set()) or set())
                    reservations.discard(reserved_name)
                    self._emoji_name_reservations = reservations

                if not isinstance(data, dict) or not data.get("id"):
                    return None
                created = {
                    "id": str(data.get("id")),
                    "name": str(data.get("name") or reserved_name),
                    "animated": bool(data.get("animated")),
                }
                items = list(getattr(self, "_application_emoji_items", []) or [])
                items.append(created)
                self._application_emoji_items = items
                names = set(getattr(self, "_application_emoji_names", set()) or set())
                names.add(created["name"])
                self._application_emoji_names = names
                self._application_emoji_count = current_count + 1
                self._application_emoji_state_at = time.monotonic()
                return created
        except Exception as exc:
            if reserved_name:
                reservations = set(getattr(self, "_emoji_name_reservations", set()) or set())
                reservations.discard(reserved_name)
                self._emoji_name_reservations = reservations
            code = int(getattr(exc, "code", 0) or 0)
            if code == 30008:
                self._application_emoji_state_at = 0.0
            log.warning("não consegui criar application emoji temporário de boas-vindas: %r", exc)
            return None

    async def _record_temp_emoji(
        self,
        *,
        guild_id: int,
        member_id: int,
        emoji: dict[str, Any],
        message_id: int = 0,
        preview: bool = False,
    ) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            log.warning("não registrei application emoji de boas-vindas: settings_db indisponível")
            return False
        emoji_id = str(emoji.get("id") or "")
        if not re.fullmatch(r"\d{15,25}", emoji_id):
            return False
        now = datetime.now(timezone.utc)
        doc = {
            "type": WELCOME_DOC_EMOJI,
            "guild_id": int(guild_id or 0),
            # A coleção settings possui índice único em (guild_id, user_id, type).
            # Usar o snowflake do próprio emoji impede que apenas o primeiro emoji do
            # servidor seja persistido com user_id=null.
            "user_id": int(emoji_id),
            "member_id": int(member_id or 0),
            "message_id": int(message_id or 0),
            "emoji_id": emoji_id,
            "emoji_name": str(emoji.get("name") or ""),
            "animated": bool(emoji.get("animated")),
            "preview": bool(preview),
            "created_at": now,
            "delete_after": self._next_welcome_midnight_utc(now),
            "status": "active",
        }
        try:
            await db.coll.update_one(
                {"type": WELCOME_DOC_EMOJI, "guild_id": doc["guild_id"], "user_id": doc["user_id"]},
                {"$set": doc, "$unset": {"deleted_at": "", "delete_reason": ""}},
                upsert=True,
            )
            return True
        except Exception as exc:
            log.warning("não consegui registrar application emoji temporário %s: %r", emoji_id, exc)
            return False

    def _temp_emoji_ids_from_config(self, cfg: dict[str, Any] | None) -> list[str]:
        result: list[str] = []
        for raw in (cfg or {}).get(WELCOME_TEMP_EMOJI_IDS_KEY, []) or []:
            emoji_id = str(raw or "")
            if re.fullmatch(r"\d{15,25}", emoji_id) and emoji_id not in result:
                result.append(emoji_id)
        return result

    async def _remove_temp_emoji_records(self, emoji_ids: list[str]) -> None:
        ids = [str(raw) for raw in emoji_ids if re.fullmatch(r"\d{15,25}", str(raw or ""))]
        if not ids:
            return
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        with contextlib.suppress(Exception):
            await db.coll.delete_many({"type": WELCOME_DOC_EMOJI, "emoji_id": {"$in": ids}})

    async def _discard_temp_emojis(self, emoji_ids: list[str], *, reason: str) -> None:
        ids = []
        for raw in emoji_ids or []:
            emoji_id = str(raw or "")
            if re.fullmatch(r"\d{15,25}", emoji_id) and emoji_id not in ids:
                ids.append(emoji_id)
        if not ids:
            return
        deleted: list[str] = []
        failed = 0
        for emoji_id in ids:
            if await self._delete_application_emoji(emoji_id):
                deleted.append(emoji_id)
            else:
                failed += 1
        if deleted:
            await self._remove_temp_emoji_records(deleted)
        log.debug(
            "[welcome-emojis] descarte reason=%s deleted=%s failed=%s",
            reason,
            len(deleted),
            failed,
        )

    async def _prepare_decorative_emojis(self, config: dict[str, Any], *, member: discord.Member | None, mode: str, dm: bool = False, invite_info: dict[str, Any] | None = None, preview: bool = False) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        cfg.pop(WELCOME_TEMP_EMOJI_IDS_KEY, None)
        if dm or member is None or not bool(cfg.get("decorative_emoji_enabled", False)):
            return cfg
        effective_limit = await self._decorative_emoji_limit_for_member(member)
        emojis = self._emoji_tokens_from_config(cfg, mode=mode, dm=dm, limit=effective_limit)
        if not emojis:
            return cfg

        color_hex = _parse_hex((self._normalize_embed_config(cfg.get("embed")).get("color") if mode == "embed" else cfg.get("accent_color")) or cfg.get("accent_color") or DEFAULT_ACCENT)
        base_rgb = self._rgb_from_hex(color_hex)
        avatar_palette = await self._member_avatar_palette(member, color_hex, limit=6)
        emoji_palette = self._subtle_emoji_palette(base_rgb, avatar_palette)
        emoji_palette_hex = self._hex_palette_from_rgb(emoji_palette)

        # O worker pode devolver só parte dos emojis. O que faltar é processado
        # localmente; qualquer falha individual mantém o emoji original completo.
        processed_by_key: dict[str, dict[str, Any]] = {}
        worker_items = await self._worker_recolor_emojis(emojis, color_hex=color_hex, palette_hex=emoji_palette_hex, limit=effective_limit, monochrome=self._palette_is_mostly_monochrome(emoji_palette))
        for item in worker_items or []:
            key = str(item.get("key") or "")
            if key:
                processed_by_key[key] = item

        missing = [emoji for emoji in emojis if str(emoji.get("key") or "") not in processed_by_key]
        if missing:
            local_items = await self._local_recolor_emojis(missing, color_hex=color_hex, palette=emoji_palette, limit=effective_limit)
            for item in local_items or []:
                key = str(item.get("key") or "")
                if key and key not in processed_by_key:
                    processed_by_key[key] = item

        if not processed_by_key:
            return cfg

        replacements: dict[str, str] = {}
        created_ids: list[str] = []
        replaced_keys: set[str] = set()
        try:
            for original in emojis:
                key = str(original.get("key") or "")
                item = processed_by_key.get(key)
                if not item:
                    continue
                emoji_id = str(original.get("id") or item.get("id") or "")
                if not re.fullmatch(r"\d{15,25}", emoji_id):
                    continue
                normalized_item = await self._normalize_emoji_upload_item(item)
                if normalized_item is None:
                    continue
                created = await self._create_application_emoji(
                    name=str(normalized_item.get("name") or item.get("name") or original.get("name") or "emoji"),
                    data_b64=str(normalized_item.get("data_b64") or ""),
                    fmt=str(normalized_item.get("format") or "png"),
                )
                if not created:
                    continue
                created_id = str(created.get("id") or "")
                tracked = await self._record_temp_emoji(
                    guild_id=int(getattr(member.guild, "id", 0) or 0),
                    member_id=int(getattr(member, "id", 0) or 0),
                    emoji=created,
                    preview=preview,
                )
                if not tracked:
                    await self._delete_application_emoji(created_id)
                    continue

                animated = bool(created.get("animated")) or str(normalized_item.get("format") or "").lower() == "gif"
                replacement = f"<a:{created.get('name')}:{created_id}>" if animated else f"<:{created.get('name')}:{created_id}>"
                replacements[f"id:{emoji_id}"] = replacement
                raws = item.get("raw_variants") if isinstance(item.get("raw_variants"), list) else original.get("raw_variants")
                if not isinstance(raws, list) or not raws:
                    raws = [item.get("raw") or original.get("raw")]
                for raw in [str(raw or "") for raw in raws if str(raw or "")]:
                    replacements[raw] = replacement
                created_ids.append(created_id)
                replaced_keys.add(key)
        except asyncio.CancelledError:
            if created_ids:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._discard_temp_emojis(created_ids, reason="prepare_failed"))
            raise
        except Exception:
            if created_ids:
                await self._discard_temp_emojis(created_ids, reason="prepare_failed")
            raise

        if not replacements:
            return cfg
        cfg = self._replace_emoji_tokens_in_config(cfg, replacements, mode=mode, dm=dm)
        cfg[WELCOME_TEMP_EMOJI_IDS_KEY] = list(created_ids)
        reused = sum(max(0, int(item.get("occurrences") or 1) - 1) for item in emojis if str(item.get("key") or "") in replaced_keys)
        log.debug(
            "[welcome-emojis] unique=%s created=%s reused=%s fallback=%s preview=%s",
            len(emojis),
            len(created_ids),
            reused,
            max(0, len(emojis) - len(created_ids)),
            bool(preview),
        )
        return cfg

    async def _delete_application_emoji(self, emoji_id: str) -> bool:
        try:
            parsed_id = int(str(emoji_id or ""))
        except Exception:
            return False
        lock = getattr(self, "_emoji_api_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._emoji_api_lock = lock
        try:
            app_id = await self._application_id()
            from discord.http import Route
            request = getattr(getattr(self.bot, "http", None), "request", None)
            if not callable(request):
                return False
            async with lock:
                await request(Route("DELETE", "/applications/{application_id}/emojis/{emoji_id}", application_id=app_id, emoji_id=parsed_id))
                current_count = getattr(self, "_application_emoji_count", None)
                if isinstance(current_count, int) and current_count > 0:
                    self._application_emoji_count = current_count - 1
                items = list(getattr(self, "_application_emoji_items", []) or [])
                self._application_emoji_items = [item for item in items if str(item.get("id") or "") != str(parsed_id)]
                self._application_emoji_state_at = time.monotonic()
            return True
        except discord.NotFound:
            self._application_emoji_state_at = 0.0
            return True
        except Exception as exc:
            log.debug("não consegui apagar emoji temporário de boas-vindas %s: %r", emoji_id, exc)
            return False

    async def _purge_temp_emojis_once(self) -> None:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        now = datetime.now(timezone.utc)
        deleted = 0
        missing_id = 0
        failed = 0
        try:
            cursor = db.coll.find(
                {"type": WELCOME_DOC_EMOJI, "status": "active", "delete_after": {"$lte": now}},
                {"_id": 1, "emoji_id": 1},
            )
            async for doc in cursor:
                emoji_id = str(doc.get("emoji_id") or "")
                if not re.fullmatch(r"\d{15,25}", emoji_id):
                    missing_id += 1
                    await db.coll.delete_one({"_id": doc.get("_id")})
                    continue
                ok = await self._delete_application_emoji(emoji_id)
                if ok:
                    deleted += 1
                    await db.coll.delete_one({"_id": doc.get("_id")})
                else:
                    failed += 1
            # Remove registros encerrados pelo mecanismo antigo para a coleção não
            # crescer indefinidamente após cada limpeza diária.
            await db.coll.delete_many({"type": WELCOME_DOC_EMOJI, "status": "deleted"})
            if deleted or missing_id or failed:
                log.info(
                    "[welcome-emojis] limpeza da meia-noite deleted=%s missing=%s failed=%s",
                    deleted,
                    missing_id,
                    failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("purge de emojis temporários de boas-vindas falhou: %r", exc)

    async def _purge_legacy_welcome_emojis_once(self) -> None:
        try:
            items = await self._application_emojis_snapshot(force=True)
        except Exception as exc:
            log.warning("não consegui listar application emojis antigos de boas-vindas: %r", exc)
            return
        legacy = [item for item in items if str(item.get("name") or "").startswith(WELCOME_LEGACY_EMOJI_PREFIX)]
        if not legacy:
            return
        log.info("[welcome-emojis] removendo %s emoji(s) legado(s) com prefixo %s", len(legacy), WELCOME_LEGACY_EMOJI_PREFIX)
        deleted_ids: list[str] = []
        failed = 0
        for index, item in enumerate(legacy, start=1):
            emoji_id = str(item.get("id") or "")
            if emoji_id and await self._delete_application_emoji(emoji_id):
                deleted_ids.append(emoji_id)
            else:
                failed += 1
            if index % 100 == 0:
                log.info("[welcome-emojis] migração antiga %s/%s", index, len(legacy))
            await asyncio.sleep(0.05)
        if deleted_ids:
            await self._remove_temp_emoji_records(deleted_ids)
        log.info("[welcome-emojis] migração antiga concluída deleted=%s failed=%s", len(deleted_ids), failed)

    async def _emoji_midnight_purge_loop(self) -> None:
        try:
            await asyncio.sleep(10)
            await self._purge_legacy_welcome_emojis_once()
            while True:
                try:
                    await self._purge_temp_emojis_once()
                    now = datetime.now(timezone.utc)
                    midnight = self._next_welcome_midnight_utc(now)
                    await asyncio.sleep(max(1.0, (midnight - now).total_seconds()) + 1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("ciclo de limpeza de application emojis falhou; tentando novamente: %r", exc)
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
