"""Compose the single audio filter accepted by FFmpeg without losing settings."""
from functools import lru_cache
import re
import shlex


@lru_cache(maxsize=128)
def compose_audio_filters(options: str, effect_filter: str) -> tuple[str, str]:
    tokens = shlex.split(options)
    kept: list[str] = []
    configured_filter = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, separator, value = token.partition("=")
        if name == "-af" or re.fullmatch(r"-filter:a(?::\d+)?", name):
            if not separator:
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"Missing audio filter after {name}")
                value = tokens[index]
            # FFmpeg uses the last filter for an output stream. Preserve that
            # behavior, then append the personal TTS effects to the same graph.
            configured_filter = value
        else:
            kept.append(token)
        index += 1
    if not effect_filter:
        return options, configured_filter
    combined = ",".join(part for part in (configured_filter, effect_filter) if part)
    kept.extend(("-af", combined))
    return shlex.join(kept), combined
