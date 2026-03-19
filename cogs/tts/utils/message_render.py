from __future__ import annotations

import re
from typing import Callable

from ..common import (
    _replace_custom_emojis_for_tts,
    _expand_abbreviations_for_tts,
    USER_MENTION_PATTERN,
    ROLE_MENTION_PATTERN,
    CHANNEL_MENTION_PATTERN,
    URL_PATTERN,
)
from .text import (
    tts_attachment_descriptions,
)


def append_tts_descriptions(text: str, descriptions: list[str], *, normalize_spaces: Callable[[str], str]) -> str:
    text = normalize_spaces(text)
    descriptions = [normalize_spaces(item) for item in (descriptions or []) if normalize_spaces(item)]
    if not descriptions:
        return text
    suffix = ". ".join(descriptions)
    if not text:
        return suffix
    if text.endswith((".", "!", "?", "…")):
        return f"{text} {suffix}"
    return f"{text}. {suffix}"


def render_message_tts_text(
    message,
    raw_text: str,
    *,
    guild_id: int | None,
    user_reference: Callable,
    role_reference: Callable,
    channel_reference: Callable,
    link_reference: Callable,
    normalize_spaces: Callable[[str], str],
    image_extensions: tuple[str, ...],
    video_extensions: tuple[str, ...],
) -> str:
    text = _replace_custom_emojis_for_tts(raw_text)
    attachments = attachments

    has_mentions_or_links = any(token in text for token in ("<@", "<@&", "<#", "http://", "https://", "www."))
    if not has_mentions_or_links:
        text = _expand_abbreviations_for_tts(text)
        if not attachments:
            return text
        descriptions = tts_attachment_descriptions(
            attachments,
            image_extensions=image_extensions,
            video_extensions=video_extensions,
        )
        return append_tts_descriptions(text, descriptions, normalize_spaces=normalize_spaces)

    guild = getattr(message, "guild", None)
    mentions = {int(getattr(member, "id", 0)): member for member in (getattr(message, "mentions", None) or []) if getattr(member, "id", None) is not None}
    role_mentions = {int(getattr(role, "id", 0)): role for role in (getattr(message, "role_mentions", None) or []) if getattr(role, "id", None) is not None}
    channel_mentions = {
        int(getattr(channel, "id", 0)): channel
        for channel in (getattr(message, "channel_mentions", None) or [])
        if getattr(channel, "id", None) is not None
    }

    def replace_user(match: re.Match[str]) -> str:
        member_id = int(match.group(1))
        member = mentions.get(member_id)
        if member is None and guild is not None:
            member = guild.get_member(member_id)
        return user_reference(member, guild_id=guild_id)

    def replace_role(match: re.Match[str]) -> str:
        role_id = int(match.group(1))
        role = role_mentions.get(role_id)
        if role is None and guild is not None:
            role = guild.get_role(role_id)
        return role_reference(role)

    def replace_channel(match: re.Match[str]) -> str:
        channel_id = int(match.group(1))
        channel = channel_mentions.get(channel_id)
        if channel is None and guild is not None:
            channel = guild.get_channel(channel_id)
        return channel_reference(channel)

    def replace_url(match: re.Match[str]) -> str:
        return link_reference(match.group(0), guild=guild)

    text = USER_MENTION_PATTERN.sub(replace_user, text)
    text = ROLE_MENTION_PATTERN.sub(replace_role, text)
    text = CHANNEL_MENTION_PATTERN.sub(replace_channel, text)
    text = URL_PATTERN.sub(replace_url, text)
    text = _expand_abbreviations_for_tts(text)

    descriptions = tts_attachment_descriptions(
        attachments,
        image_extensions=image_extensions,
        video_extensions=video_extensions,
    )
    return append_tts_descriptions(text, descriptions, normalize_spaces=normalize_spaces)
