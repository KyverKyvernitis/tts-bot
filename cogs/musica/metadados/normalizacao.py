from __future__ import annotations

import html
import re
import unicodedata

BAD_VERSION_WORDS = {
    "slowed",
    "slow",
    "nightcore",
    "speed up",
    "sped up",
    "8d",
    "karaoke",
    "instrumental",
    "cover",
    "remix",
    "live",
    "lyrics",
    "lyric",
    "bass boosted",
    "extended",
    "edit",
    "reverb",
}

GOOD_VERSION_PHRASES = (
    "official audio",
    "official video",
    "official music video",
    "audio oficial",
    "video oficial",
)

OFFICIAL_CHANNEL_HINTS = ("official", "vevo", "topic", "records", "music")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^]]*]", " ", value)
    value = re.sub(
        r"\b(official|music|video|audio|lyrics?|lyric|visualizer|remaster(?:ed)?|hd|hq|4k|mv|clip)\b",
        " ",
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_key(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


def is_bad_match_title(title: str, *, query: str = "") -> bool:
    raw = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", title or "").lower())
    normalized = normalize_text(title)
    q_raw = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", query or "").lower())
    q = normalize_text(query)
    if not q and not q_raw:
        return any(word in normalized or word in raw for word in BAD_VERSION_WORDS)
    return any(
        (word in normalized or word in raw) and word not in q and word not in q_raw
        for word in BAD_VERSION_WORDS
    )


def title_quality_score(title: str, *, query: str = "", channel: str = "") -> float:
    raw_title = re.sub(r"\s+", " ", (title or "").lower())
    raw_channel = re.sub(r"\s+", " ", (channel or "").lower())
    score = 0.0
    if any(phrase in raw_title for phrase in GOOD_VERSION_PHRASES):
        score += 16
    if any(hint in raw_channel for hint in OFFICIAL_CHANNEL_HINTS):
        score += 5
    if is_bad_match_title(title, query=query):
        score -= 26
    if "lyrics" in raw_title or "lyric" in raw_title:
        score -= 8
    return score


def parse_iso8601_duration(value: str) -> float | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def clean_metadata_title(text: str) -> str:
    value = html.unescape(re.sub(r"\s+", " ", text or "").strip())
    if not value:
        return ""
    value = re.sub(
        r"\s*[-|•]\s*(YouTube|Spotify|Apple Music|Deezer)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.replace(" - song and lyrics by ", " ")
    value = value.replace(" | Spotify", "")
    value = re.sub(r"\bListen to\b", "", value, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", value).strip(" -|•\t\n\r")


def unique_queries(*queries: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        clean = re.sub(r"\s+", " ", (query or "").strip())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out
