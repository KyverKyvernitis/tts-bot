"""Pure TTS selection and cache contracts; runtime state and IO stay with callers."""
from __future__ import annotations

import contextlib
import hashlib
import re
from typing import Any


def normalize_engine(raw: Any, *, default: str = "gtts") -> str:
    value = str(raw or default).strip().lower().replace("-", "_") or default
    aliases = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "edge_tts": "edge", "android": "android_native", "android_tts": "android_native", "native": "android_native", "native_android": "android_native", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto"}
    return aliases.get(value, value)


def available_engines(deps: dict[str, Any]) -> list[str]:
    engines: list[str] = []
    if deps.get("teto_tts"):
        engines.append("teto")
    if deps.get("android_native_tts"):
        engines.append("android_native")
    if deps.get("edge_tts"):
        engines.append("edge")
    if deps.get("gtts"):
        engines.append("gtts")
    return engines


def preferred_engine(available: list[str], *, requested: Any) -> str:
    requested = normalize_engine(requested, default="auto")
    if requested != "auto" and requested in available:
        return requested
    for candidate in ("android_native", "edge", "gtts"):
        if candidate in available:
            return candidate
    return ""


def engine_order(body: dict[str, Any], available: list[str], *, preferred_default: Any) -> list[str]:
    requested = normalize_engine(body.get("engine"))
    preferred = normalize_engine(body.get("preferred_engine") or preferred_default, default="auto")
    fallback = normalize_engine(body.get("fallback_engine"))
    order: list[str] = []
    if requested == "teto":
        # Explicit Teto takes priority over the global preference.
        order.append("teto")
    elif preferred != "auto":
        order.append(preferred)
    order.append(requested)
    if fallback != requested:
        order.append(fallback)
    order.extend(("android_native", "edge", "gtts"))
    deduped: list[str] = []
    for engine in order:
        if engine not in {"teto", "android_native", "edge", "gtts"}:
            continue
        if engine not in available:
            continue
        if engine not in deduped:
            deduped.append(engine)
    return deduped


def normalize_edge_rate(raw: Any) -> str:
    value = str(raw or "").strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
    if value.endswith("%"):
        value = value[:-1]
    if not value:
        return "+0%"
    if value[0] not in "+-":
        value = f"+{value}"
    sign, number = value[0], value[1:]
    if not number.isdigit():
        return "+0%"
    return f"{sign}{number}%"


def normalize_edge_pitch(raw: Any) -> str:
    value = str(raw or "").strip().replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
    if value.lower().endswith("hz"):
        value = value[:-2]
    if not value:
        return "+0Hz"
    if value[0] not in "+-":
        value = f"+{value}"
    sign, number = value[0], value[1:]
    if not number.isdigit():
        return "+0Hz"
    return f"{sign}{number}Hz"


def normalize_gtts_language(raw: Any) -> str:
    language = str(raw or "pt").strip().lower().replace("_", "-") or "pt"
    if language == "pt-br":
        language = "pt"
    return language


def sanitize_cache_key(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    key = re.sub(r"[^a-z0-9_\-]", "", key)
    if len(key) < 16:
        raise RuntimeError("cache_key inválida/curta")
    return key[:96]


def normalize_cache_format(raw: Any) -> str:
    fmt = str(raw or "mp3").strip().lower().replace(".", "")
    if fmt in {"wav", "wave"}:
        return "wav"
    if fmt in {"ogg", "opus"}:
        return "ogg"
    return "mp3"


def standard_cache_key(body: dict[str, Any], *, engine: str, sanitize_key, normalize_rate,
                       normalize_pitch, normalize_language, teto_fingerprint: str,
                       teto_base_pitch: str) -> str:
    normalized_engine = normalize_engine(engine or body.get("engine"))
    requested_engine = normalize_engine(body.get("engine"), default=normalized_engine)
    provided = str(body.get("cache_key") or "").strip()
    if provided and requested_engine == normalized_engine and normalized_engine != "teto":
        with contextlib.suppress(Exception):
            return sanitize_key(provided)
    text = str(body.get("text") or "").strip()
    if normalized_engine == "teto":
        fingerprint = str(teto_fingerprint or "unavailable")
        voice = str(body.get("voice") or "kasane-teto-standard").strip() or "kasane-teto-standard"
        language = str(body.get("language") or "pt-BR").strip() or "pt-BR"
        base_pitch = str(teto_base_pitch or "C4")
        payload = f"teto|{fingerprint}|{voice}|{language}|{base_pitch}|{text}"
    elif normalized_engine == "android_native":
        language = str(body.get("language") or body.get("fallback_language") or "pt-BR").strip().replace("_", "-") or "pt-BR"
        voice = str(body.get("voice") or "auto").strip() or "auto"
        rate = str(body.get("rate") or "1.0").strip() or "1.0"
        pitch = str(body.get("pitch") or "1.0").strip() or "1.0"
        payload = f"android_native|{language}|{voice}|{rate}|{pitch}|{text}"
    elif normalized_engine == "edge":
        voice = str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
        rate = normalize_rate(body.get("rate"))
        pitch = normalize_pitch(body.get("pitch"))
        payload = f"edge|{voice}|{rate}|{pitch}|{text}"
    else:
        language = normalize_language(body.get("language") or body.get("fallback_language"))
        payload = f"gtts|{language}|{body.get('tld') or 'com'}|{text}"
    return hashlib.sha256(("tts-v2|" + payload).encode("utf-8")).hexdigest()


def cache_mode_allows_read(body: dict[str, Any]) -> bool:
    mode = str(body.get("cache_mode") or "prefer").strip().lower()
    return mode not in {"0", "false", "off", "disabled", "none", "bypass", "refresh"}


def cache_mode_allows_store(body: dict[str, Any]) -> bool:
    mode = str(body.get("cache_mode") or "prefer").strip().lower()
    return mode not in {"0", "false", "off", "disabled", "none", "bypass", "no_store"}
