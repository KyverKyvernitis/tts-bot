from __future__ import annotations

import colorsys
from io import BytesIO
from typing import Iterable

try:
    from PIL import Image
except Exception:  # pragma: no cover - runtime guard only
    Image = None


def clean_hex(value: str | None, fallback: str = "#ffffff") -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback.lower()
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if len(raw) != 7:
        return fallback.lower()
    try:
        int(raw[1:], 16)
    except Exception:
        return fallback.lower()
    return raw.lower()


def rgb_from_hex(value: str | None, fallback: str = "#ffffff") -> tuple[int, int, int]:
    value = clean_hex(value, fallback)
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _clamp_channel(value: float | int) -> int:
    return max(0, min(255, int(round(float(value)))))


def mix_rgb(left: tuple[int, int, int], right: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, float(ratio)))
    return (
        _clamp_channel(left[0] + (right[0] - left[0]) * t),
        _clamp_channel(left[1] + (right[1] - left[1]) * t),
        _clamp_channel(left[2] + (right[2] - left[2]) * t),
    )


def adjust_rgb_hsv(
    rgb: tuple[int, int, int],
    *,
    sat_mul: float = 1.0,
    val_mul: float = 1.0,
    hue_shift: float = 0.0,
) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    h = (h + float(hue_shift)) % 1.0
    s = max(0.0, min(1.0, s * float(sat_mul)))
    v = max(0.0, min(1.0, v * float(val_mul)))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return _clamp_channel(r * 255), _clamp_channel(g * 255), _clamp_channel(b * 255)


def subtle_palette(base_rgb: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    palette = [base_rgb]
    palette.append(adjust_rgb_hsv(base_rgb, sat_mul=0.92, val_mul=1.24))
    palette.append(adjust_rgb_hsv(base_rgb, sat_mul=1.06, val_mul=0.68))
    palette.append(adjust_rgb_hsv(base_rgb, sat_mul=0.98, val_mul=1.08, hue_shift=0.025))
    palette.append(adjust_rgb_hsv(base_rgb, sat_mul=1.04, val_mul=0.86, hue_shift=-0.025))
    return palette


def recolor_rgba_image(img, rgb: tuple[int, int, int], palette: Iterable[tuple[int, int, int]] | None = None):
    """Recolore imagem RGBA preservando alpha e contraste.

    É uma versão isolada do método usado nos emojis decorativos da cog welcome:
    luminância controla sombra/luz, e a cor original só influencia nuances leves.
    """
    if Image is None:
        raise RuntimeError("Pillow indisponível")
    img = img.convert("RGBA")
    px = img.load()
    base = rgb
    usable_palette = list(palette or subtle_palette(base))
    light = usable_palette[1] if len(usable_palette) > 1 else adjust_rgb_hsv(base, sat_mul=0.92, val_mul=1.22)
    dark = usable_palette[2] if len(usable_palette) > 2 else adjust_rgb_hsv(base, sat_mul=1.04, val_mul=0.68)
    accents = usable_palette[3:] or [base]
    width, height = img.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = px[x, y]
            if a < 8:
                continue
            lum = max(0.0, min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0))
            if lum < 0.50:
                target = mix_rgb(dark, base, lum / 0.50)
            else:
                target = mix_rgb(base, light, (lum - 0.50) / 0.50)
            try:
                oh, osat, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                if accents and osat > 0.08:
                    accent = accents[(int(oh * 12) + (x // 24) + (y // 24)) % len(accents)]
                    target = mix_rgb(target, accent, 0.10)
            except Exception:
                pass
            px[x, y] = (target[0], target[1], target[2], a)
    return img


def _save_png(img, *, max_side: int = 256, max_bytes: int = 256_000) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow indisponível")
    rgba = img.convert("RGBA")
    width, height = rgba.size
    if max(width, height) > max_side:
        scale = max_side / max(width, height)
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        rgba = rgba.resize((max(1, int(width * scale)), max(1, int(height * scale))), resampling)
    for colors in (None, 128, 96, 64):
        candidate = rgba
        if colors is not None:
            candidate = rgba.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors).convert("RGBA")
        out = BytesIO()
        candidate.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= max_bytes:
            return data
    return data


def normalize_icon_png(raw: bytes, *, max_side: int = 256, max_bytes: int = 256_000) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow indisponível")
    with Image.open(BytesIO(raw)) as img:
        return _save_png(img.convert("RGBA"), max_side=max_side, max_bytes=max_bytes)


def recolor_icon_bytes(raw: bytes, color_hex: str, *, max_side: int = 256, max_bytes: int = 256_000) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow indisponível")
    rgb = rgb_from_hex(color_hex)
    with Image.open(BytesIO(raw)) as img:
        recolored = recolor_rgba_image(img.convert("RGBA"), rgb, subtle_palette(rgb))
        return _save_png(recolored, max_side=max_side, max_bytes=max_bytes)
