"""Explicit guild canaries for the Edge/gTTS startup changes."""
from functools import lru_cache
import hashlib
import os


_FLAGS = {
    "route": "TTS_NATIVE_EXISTING_VOICE_ENABLED",
    "segments": "TTS_FIRST_AUDIO_SEGMENTS",
    "async_cache": "TTS_FIRST_AUDIO_ASYNC_CACHE",
}


@lru_cache(maxsize=4)
def _guilds(config: str) -> frozenset[str]:
    return frozenset(value for token in config.split(",") if (value := token.strip()).isascii() and value.isdecimal())


def enabled_for(guild_id: int, feature: str) -> bool:
    return (str(guild_id) in _guilds(os.getenv("TTS_FIRST_AUDIO_GUILDS", ""))
            and os.getenv(_FLAGS[feature], "").strip().lower() in {"1", "true", "on", "yes"})


def segments_for(item) -> bool:
    from .providers_rust import enabled, native_module

    if (str(item.engine).lower() not in {"edge", "gtts"} or not enabled()
            or not enabled_for(item.guild_id, "segments")):
        return False
    return getattr(native_module(), "NATIVE_STARTUP_SEGMENTS_VERSION", 0) == 1


def segmented_cache_key(key: str) -> str:
    return hashlib.sha256(b"tts-startup-segments-v1\0" + key.encode("utf-8")).hexdigest()
