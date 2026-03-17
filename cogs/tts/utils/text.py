from __future__ import annotations

from urllib.parse import urlparse
from typing import Callable


def tts_user_reference(member, *, resolver: Callable, guild_id: int | None = None) -> str:
    spoken, _ = resolver(member, guild_id=guild_id)
    return spoken


def tts_role_reference(role, *, normalize_spaces: Callable[[str], str], looks_pronounceable_for_tts: Callable[[str], bool], speech_name: Callable[[str], str]) -> str:
    name = normalize_spaces(getattr(role, "name", None) or "")
    if looks_pronounceable_for_tts(name):
        spoken = speech_name(name)
        if spoken:
            return f"cargo {spoken}"
    return "cargo do discord"


def tts_channel_reference(channel, *, normalize_spaces: Callable[[str], str], looks_pronounceable_for_tts: Callable[[str], bool], speech_name: Callable[[str], str]) -> str:
    name = normalize_spaces(getattr(channel, "name", None) or "")
    if looks_pronounceable_for_tts(name):
        spoken = speech_name(name)
        if spoken:
            return f"canal {spoken}"
    return "canal do discord"


def tts_link_reference(
    url: str,
    *,
    guild=None,
    discord_channel_url_pattern,
    channel_reference: Callable,
    extract_primary_domain: Callable[[str], str],
    looks_pronounceable_for_tts: Callable[[str], bool],
    speech_name: Callable[[str], str],
) -> str:
    cleaned_url = str(url or "").strip().rstrip(".,!?)]}")
    match = discord_channel_url_pattern.fullmatch(cleaned_url)
    if match and guild is not None:
        channel_id = int(match.group(2))
        channel = guild.get_channel(channel_id)
        return channel_reference(channel)

    try:
        parsed = urlparse(cleaned_url)
    except Exception:
        return "link"

    domain = extract_primary_domain(parsed.hostname or "")
    if looks_pronounceable_for_tts(domain):
        spoken = speech_name(domain)
        if spoken:
            return f"link do {spoken}"
    return "link"


def tts_attachment_descriptions(attachments, *, image_extensions: tuple[str, ...], video_extensions: tuple[str, ...]) -> list[str]:
    descriptions: list[str] = []
    for attachment in attachments or []:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        filename = str(getattr(attachment, "filename", "") or "").lower()
        if content_type == "image/gif" or filename.endswith(".gif"):
            descriptions.append("Anexo em GIF")
        elif content_type.startswith("image/") or filename.endswith(image_extensions):
            descriptions.append("Anexo de imagem")
        elif content_type.startswith("video/") or filename.endswith(video_extensions):
            descriptions.append("Anexo de vídeo")
    return descriptions
