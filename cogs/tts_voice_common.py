import inspect
import contextlib
import asyncio
import time
import re
import weakref
import unicodedata
from urllib.parse import urlparse
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from google.cloud import texttospeech_v1 as google_texttospeech
except Exception:  # pragma: no cover - dependência opcional em tempo de import
    google_texttospeech = None

import config
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin

from typing import Callable


_TTS_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in getattr(config, "GUILD_IDS", [])]


def _guild_scoped():
    return app_commands.guilds(*_TTS_GUILD_OBJECTS) if _TTS_GUILD_OBJECTS else (lambda f: f)


def _shorten(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _replace_custom_emojis_for_tts(text: str) -> str:
    return re.sub(r"<a?:([A-Za-z0-9_~]+):\d+>", lambda m: f"emoji {m.group(1)}", text)


_TTS_ABBREVIATION_MAP = {
    # comuns
    "tb": "também",
    "tbm": "também",
    "tmb": "também",
    "vc": "você",
    "vcs": "vocês",
    "pq": "porque",
    "pk": "porque",
    "q": "que",
    "blz": "beleza",
    "obg": "obrigado",
    "obgd": "obrigado",
    "pf": "por favor",
    "pfv": "por favor",
    "hj": "hoje",
    "dps": "depois",
    "gnt": "gente",
    "sdds": "saudades",
    "vdd": "verdade",
    "flw": "falou",
    "vlw": "valeu",
    "cmg": "comigo",
    "ctz": "certeza",
    "msg": "mensagem",
    # outras comuns
    "mds": "meu deus",
    "tmj": "tamo junto",
    "slk": "cê é louco",
    "pdc": "pode crer",
    "rlx": "relaxa",
    "sqn": "só que não",
    "ngm": "ninguém",
    "td": "tudo",
    "nd": "nada",
    "bjs": "beijos",
    "abs": "abraços",
    "kd": "cadê",
    "qnd": "quando",
    "fds": "foda-se",
    # ofensivas comuns
    "fdp": "filho da puta",
    "vsf": "vai se foder",
    "vtnc": "vai tomar no cu",
    "tmnc": "tomar no cu",
    "tnc": "tomar no cu",
    "pqp": "puta que pariu",
    "prr": "porra",
    "crl": "caralho",
    "krl": "caralho",
}

_TTS_ABBREVIATION_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_TTS_ABBREVIATION_MAP, key=len, reverse=True)) + r")\b",
    flags=re.IGNORECASE,
)


USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#(\d+)>")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)
DISCORD_CHANNEL_URL_PATTERN = re.compile(
    r"https?://(?:canary\.|ptb\.)?(?:www\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)(?:/\d+)?",
    flags=re.IGNORECASE,
)
_ATTACHMENT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".svg", ".avif", ".heic", ".heif")
_ATTACHMENT_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv", ".flv", ".3gp")
_COMMON_MULTI_PART_TLDS = {"com", "net", "org", "gov", "edu", "co"}


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _speech_name(text: str) -> str:
    text = _normalize_spaces(text)
    if not text:
        return ""
    text = re.sub(r"[_\-.]+", " ", text)
    return _normalize_spaces(text)


def _looks_pronounceable_for_tts(text: str) -> bool:
    text = _normalize_spaces(text)
    if not text:
        return False

    non_space = [ch for ch in text if not ch.isspace()]
    if not non_space:
        return False

    alnum_count = sum(ch.isalnum() for ch in non_space)
    friendly_count = sum(ch.isalnum() or ch in "._-" for ch in non_space)
    symbol_count = sum(unicodedata.category(ch).startswith("S") for ch in non_space)
    hard_punct = {"[", "]", "{", "}", "(", ")", "<", ">", "~", "^", "`", "|", chr(92), "/", '"', "'", "*", "=", ":", ";", "+", ","}
    hard_punct_count = sum(ch in hard_punct for ch in non_space)

    if alnum_count == 0:
        return False
    if friendly_count / max(1, len(non_space)) < 0.6:
        return False
    if symbol_count >= 1:
        return False
    if hard_punct_count >= 2:
        return False
    return True


def _extract_primary_domain(hostname: str) -> str:
    host = str(hostname or "").strip().lower().strip(".")
    if not host:
        return ""
    parts = [part for part in host.split(".") if part]
    if not parts:
        return ""
    while len(parts) > 2 and parts[0] in {"www", "m", "ptb", "canary", "cdn", "media"}:
        parts = parts[1:]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _COMMON_MULTI_PART_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def _expand_abbreviations_for_tts(text: str) -> str:
    if not text or not _TTS_ABBREVIATION_PATTERN.search(text):
        return text

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return _TTS_ABBREVIATION_MAP.get(token.lower(), token)

    return _TTS_ABBREVIATION_PATTERN.sub(repl, text)

def get_gtts_languages() -> dict[str, str]:
    try:
        from gtts.lang import tts_langs
        return tts_langs()
    except Exception:
        return {
            "pt": "Portuguese",
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "ja": "Japanese",
        }


def build_gtts_language_aliases(languages: dict[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {
        "portugues": "pt",
        "português": "pt",
        "portugues brasil": "pt",
        "português brasil": "pt",
        "pt br": "pt",
        "pt-br": "pt",
        "ptbr": "pt",
        "brasileiro": "pt",
        "ingles": "en",
        "inglês": "en",
        "espanhol": "es",
        "frances": "fr",
        "francês": "fr",
        "alemao": "de",
        "alemão": "de",
        "italiano": "it",
        "japones": "ja",
        "japonês": "ja",
    }
    for code, name in (languages or {}).items():
        code_norm = str(code or "").strip().lower()
        if not code_norm:
            continue
        aliases.setdefault(code_norm, code_norm)
        aliases.setdefault(code_norm.replace("_", "-"), code_norm)
        aliases.setdefault(code_norm.replace("-", " "), code_norm)
        name_norm = str(name or "").strip().lower()
        if name_norm:
            aliases.setdefault(name_norm, code_norm)
            aliases.setdefault(name_norm.replace("(", " ").replace(")", " ").replace("-", " ").replace("_", " ").replace("  ", " ").strip(), code_norm)
    return aliases


def validate_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value in {"edge", "gtts", "gcloud"}:
        return value
    return "gtts"
