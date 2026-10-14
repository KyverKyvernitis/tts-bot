"""Standalone CD metadata selection using Osaka's music player sources.

Only public metadata is fetched. Search scope, version/identity rejection and
view-count observations are retained; popularity never means all of YouTube.
The Termux kit bundles the unmodified player files listed in PLAYER_SOURCES.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import html
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import types
import unicodedata
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

PLAYER_SOURCES = (
    "metadados/normalizacao.py", "metadados/modelos.py",
    "metadados/fontes/youtube.py", "nucleo/modelos.py",
    "busca/normalizacao.py", "busca/atributos.py",
)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
MAX_RESPONSE = 2 * 1024 * 1024


class MetadataSearchError(RuntimeError):
    """Retryable provider failure; audit never contains URLs with credentials."""

    def __init__(self, reason: str, audit: dict | None = None):
        super().__init__(reason)
        self.audit = audit or {}


class MetadataNoMatchError(MetadataSearchError):
    """Search completed without an identified recording with known views."""


def player_modules():
    """Load only pure player modules, without Discord or player configuration."""
    here = Path(__file__).resolve().parent
    source = here / "player_metadata"
    if not all((source / name).is_file() for name in PLAYER_SOURCES):
        source = here.parents[1] / "cogs/musica"
    if not all((source / name).is_file() for name in PLAYER_SOURCES):
        raise MetadataSearchError("incomplete_player_metadata_sources")
    namespace = "_osaka_cd_player_" + hashlib.sha256(str(source).encode()).hexdigest()[:12]
    for suffix in ("", ".metadados", ".metadados.fontes", ".nucleo", ".busca"):
        name = namespace + suffix
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(source / suffix.strip(".").replace(".", "/"))]
            sys.modules[name] = package
    return (importlib.import_module(namespace + ".metadados.normalizacao"),
            importlib.import_module(namespace + ".busca.atributos"),
            importlib.import_module(namespace + ".metadados.fontes.youtube"))


def unicode_key(value: str) -> str:
    """Keep Japanese/CJK and Latin accents instead of erasing non-ASCII titles."""
    value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    value = "".join(char if char.isalnum() or unicodedata.category(char).startswith("M") else " "
                    for char in value)
    return re.sub(r"\s+", " ", value).strip()


def _contains(wanted: str, found: str, normalizer, *, title_contractions=False) -> bool:
    wanted_u, found_u = unicode_key(wanted), unicode_key(found)
    if not wanted_u or not found_u:
        return False
    if any("CJK" in unicodedata.name(c, "") or "HIRAGANA" in unicodedata.name(c, "")
           or "KATAKANA" in unicodedata.name(c, "") or "HANGUL" in unicodedata.name(c, "")
           for c in wanted_u):
        return wanted_u in found_u
    # Reuse the player's presentation-noise/accent normalization for Latin.
    wanted_tokens = set((normalizer(wanted) or wanted_u).split())
    found_tokens = set((normalizer(found) or found_u).split())
    if title_contractions:
        # A dropped final g (``Rolling`` / ``Rollin'``) is a conventional
        # English spelling, not an arbitrary stem match. Keep every other
        # token exact, exclude short words such as ``king`` / ``kin`` and
        # ``thing`` / ``thin``, and
        # never apply this equivalence to artist/performer identity.
        def spelling(token):
            return token[:-1] if re.fullmatch(r"[a-z]{3,}ing", token) else token
        wanted_tokens = {spelling(token) for token in wanted_tokens}
        found_tokens = {spelling(token) for token in found_tokens}
    return bool(wanted_tokens) and wanted_tokens <= found_tokens


def _extra_versions(title: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", title).casefold()
    result = _discussion_versions(title)
    if re.search(r"\b(?:cover(?:ed|s)?|sings|singing|copy)\b|歌\s*って\s*みた|カバー|翻唱|弹唱|彈唱|open\s*[- ]?\s*utau", raw):
        result.add("cover")
    if re.search(r"['’]s\s+sing\b", raw):
        # ``Singer's sing`` is an explicit performance label. The bare word
        # ``Sing`` may itself be a song title and is not enough to reject it.
        result.add("cover")
    if re.search(r"カラオケ|伴奏|ニコカラ|\boff[ -]*vocals?\b", raw):
        result.add("karaoke")
    if re.search(r"演奏してみた|弾いてみた|弾き語り|叩いてみた|吹いてみた|楽譜|樂譜|乐谱|"
                 r"ピアノ|クラリネット|オルゴール|八音盒|音乐盒|音樂盒|钢琴|鋼琴|吉他|"
                 r"单簧管|單簧管|\b(?:piano|guitar|clarinet|violin|flute|saxophone|drum)\b|"
                 r"\bmusic\s+box\b|\bdtx(?:hd|mania)?\b", raw):
        result.add("instrumental")
    if re.search(r"アレンジ|編曲|改编|改編|\barrang(?:e|ed|ement)(?:\s+(?:ver|version))?\b", raw):
        result.add("remix")
    if re.search(r"ライブ|演唱会|演唱會|コンサート|マジカルミライ|"
                 r"\b(?:\d+d\s*live|concert|magical mirai|livestream)\b", raw):
        result.add("live")
    if re.search(r"[\[(【]\s*(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}\s+at\s+\S", raw):
        result.add("live")
    if re.search(r"\btribute\b|トリビュート", raw):
        result.add("tribute")
    if re.search(r"\b\d+(?:[.,]\d+)?\s*bpm\b", raw):
        result.add("tempo_variant")
    if _named_versions(title):
        result.add("version_variant")
    return result


def _discussion_versions(title: str) -> set[str]:
    """Explicit discussion labels identify videos about a recording.

    Producer and song credits also appear in reactions, so title/artist matches
    do not prove that a candidate plays the requested recording. Keep these
    labels in the existing version mechanism: a song actually named Reaction
    or Analysis still matches its own title, and an explicit requested label
    or ``allow_versions`` entry can opt in to a discussion format.
    """
    raw = unicodedata.normalize("NFKC", html.unescape(str(title or ""))).casefold()
    result = set()
    if re.search(r"\b(?:reactions?|reacts?|reacting|reacted|réactions?|reaktion(?:en)?|"
                 r"reazion[ei]|reacci[oó]n(?:es)?|reaccionando|rea[cç](?:[aã]o|[oõ]es)|reagindo|"
                 r"реакци[яию])\b|\bfirst[ -]+time[ -]+(?:hearing|listening)\b|"
                 r"リアクション|反応|反[应應]|리액션|반응", raw):
        result.add("reaction")
    if re.search(r"\b(?:commentary|commentaries|coment[aá]rios?|comentarios?|"
                 r"commentaire[s]?|reviews?)\b|解[説说說]|点评|點評|해설", raw):
        result.add("commentary")
    if re.search(r"\b(?:analysis|analyses|analy[sz](?:ing|ed)|an[aá]lis[ei]s?|"
                 r"analisando|analys[ei]|analisi|разбор|анализ)\b|"
                 r"考察|解析|分析|분석", raw):
        result.add("analysis")
    return result


def _named_versions(title: str) -> set[str]:
    """Unknown named versions need explicit input, even with producer credit.

    Presentation words such as ``English Sub Ver`` keep the same recording.
    Known arrangement labels are handled by the player/extra variant rules.
    A bare unknown ``Some Ver`` is not evidence of the requested recording.
    """
    raw = unicodedata.normalize("NFKC", html.unescape(str(title or ""))).casefold()
    known = {"original", "official", "audio", "video", "mv", "pv", "lyrics", "lyric",
             "sub", "subs", "subtitle", "subtitles", "subbed", "字幕", "中文字幕",
             "cover", "live", "remix", "acoustic", "instrumental", "piano", "guitar",
             "clarinet", "violin", "flute", "saxophone", "drum", "arrange", "arranged",
             "arrangement", "karaoke", "slowed", "extended", "remaster", "remastered",
             "clean", "explicit", "edit", "reverb", "オルゴール", "ピアノ"}
    labels = set()
    for match in re.finditer(r"(?P<label>[^\W_]+)\s+(?:ver(?:sion)?|バージョン)\.?\b", raw):
        label = match.group("label")
        if label not in known:
            # Preserve a complete bracketed name, so ``Other Supernova Ver``
            # cannot satisfy a request for ``Supernova Ver`` merely by sharing
            # its last word. Unbracketed versions retain the adjacent label.
            prefix = raw[:match.start()]
            opening = max((prefix.rfind(char) for char in "([【"), default=-1)
            closing = max((prefix.rfind(char) for char in ")]】"), default=-1)
            if opening > closing:
                label = unicode_key(raw[opening + 1:match.end("label")])
            labels.add(label)
    labels.update(re.findall(r"\bver(?:sion)?\.?\s+(\d+)\b", raw))
    return labels


def _explicit_tempos(title: str) -> set[float]:
    raw = unicodedata.normalize("NFKC", str(title or "")).casefold()
    return {float(value.replace(",", "."))
            for value in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*bpm\b", raw)}


def _attributed_performers(title: str) -> set[str]:
    """Separate explicit performance/by credits from producer/author credits.

    Plain ``feat`` is deliberately insufficient: original releases often name
    a singer distinct from their producer. A title crediting the expected
    producer cannot, however, make an explicitly different performance match.
    """
    raw = unicodedata.normalize("NFKC", html.unescape(str(title or ""))).casefold()
    result = set()
    for match in re.finditer(r"\bby\s+([^\[\](){}【】\n]+)", raw):
        prefix = raw[:match.start()]
        # These roles identify the work or its presentation, not its singer.
        if re.search(r"\b(?:lyrics?|written|composed|produced|music|song|original|art|"
                     r"pv|animation|video|illustration|translation)\s*$", prefix):
            continue
        performer = re.split(r"\s+(?:/|\||[-–—])\s+", match.group(1), maxsplit=1)[0]
        value = unicode_key(performer)
        if value:
            result.add(value)
    for match in re.finditer(r"\b(?:sings|singing)\b", raw):
        # Credits commonly use ``Singer Sings Song`` rather than ``by``.
        performer = re.split(r"[\[\](){}【】/|]|\s+[-–—]\s+", raw[:match.start()])[-1]
        value = unicode_key(performer)
        if value:
            result.add(value)
    for match in re.finditer(r"([^\[\](){}【】/|\n]+?)['’]s\s+sing\b", raw):
        performer = re.split(r"\s+[-–—]\s+", match.group(1))[-1]
        value = unicode_key(performer)
        if value:
            result.add(value)
    return result


def match_candidate(title: str, artist: str, candidate: dict, *, aliases=(),
                    artist_aliases=(), allow_versions=(), duration=None) -> tuple[bool, list[str]]:
    normal, attributes, _ = player_modules()
    found_title = normal.clean_metadata_title(str(candidate.get("title") or ""))
    reasons = []
    if not any(_contains(alias, found_title, normal.normalize_text, title_contractions=True)
               for alias in (title, *aliases) if alias):
        reasons.append("title_mismatch")
    combined = found_title + " " + str(candidate.get("channel") or candidate.get("artist") or "")
    if not artist and not artist_aliases:
        reasons.append("artist_unspecified")
    elif not any(_contains(alias, combined, normal.normalize_text) for alias in (artist, *artist_aliases) if alias):
        reasons.append("artist_mismatch")
    expected = set(attributes.detectar_variantes(title)) | _extra_versions(title) | set(allow_versions)
    actual = set(attributes.detectar_variantes(found_title)) | _extra_versions(found_title)
    # Requesting one unknown named version must not authorize all other names.
    if ("version_variant" not in allow_versions
            and _named_versions(found_title) - _named_versions(title)):
        expected.discard("version_variant")
    if "tempo_variant" not in allow_versions:
        if _explicit_tempos(found_title) - _explicit_tempos(title):
            expected.discard("tempo_variant")
    expected_performers = _attributed_performers(title)
    unexpected_performers = [performer for performer in _attributed_performers(found_title)
                            if performer not in expected_performers
                            and not any(_contains(alias, performer, normal.normalize_text)
                                        for alias in (artist, *artist_aliases) if alias)]
    if unexpected_performers:
        actual.add("performer_variant")
    # Lyrics/official audio are presentation formats, not different recordings.
    reasons.extend("unrequested_version_" + version for version in sorted(actual - expected))
    actual_duration = candidate.get("duration")
    if duration and type(actual_duration) in (int, float) and actual_duration > 0:
        if abs(float(duration) - actual_duration) > max(40, float(duration) * .25):
            reasons.append("duration_mismatch")
    if candidate.get("is_live"):
        reasons.append("live_stream")
    if not VIDEO_ID.fullmatch(str(candidate.get("video_id") or "")):
        reasons.append("invalid_video_id")
    if type(candidate.get("view_count")) is not int or candidate["view_count"] < 0:
        reasons.append("view_count_unavailable")
    return not reasons, reasons


def _candidate(raw: dict, *, source: str, query: str = "") -> dict:
    """Only persist fields needed for identity/popularity; discard stream URLs."""
    video_id = str(raw.get("video_id") or raw.get("id") or "")
    if not video_id:
        video_id = str((parse_qs(urlparse(str(raw.get("webpage_url") or "")).query).get("v") or [""])[0])
    raw_duration = raw.get("duration")
    duration = raw_duration if type(raw_duration) in (int, float) and math.isfinite(raw_duration) and raw_duration > 0 else None
    thumbnail = str(raw.get("thumbnail") or "")
    if urlparse(thumbnail).scheme != "https":
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    result = {"video_id": video_id, "title": html.unescape(str(raw.get("title") or ""))[:500],
              "channel": html.unescape(str(raw.get("channel") or raw.get("uploader") or raw.get("artist") or ""))[:250],
              "view_count": raw.get("view_count") if type(raw.get("view_count")) is int else None,
              "duration": duration, "thumbnail": thumbnail,
              "source": source, "query": query, "is_live": bool(raw.get("is_live", False))}
    if raw.get("view_count_observed_at") or raw.get("observed_at") or raw.get("checked_at"):
        result["view_count_observed_at"] = str(raw.get("view_count_observed_at") or raw.get("observed_at") or raw.get("checked_at"))
    return result


class YouTubePlayerAdapter:
    """Reuse player API search/metadata and add statistics needed by CDs."""

    def __init__(self, key: str, *, limit: int):
        _, _, youtube = player_modules()
        adapter = self

        class Provider(youtube.ProvedorYouTubeMixin):
            youtube_api_key = key

            async def _to_thread_json(self, url: str, **kwargs):
                # The public API is synchronous and already belongs in the
                # importer's worker thread. Keep the player's async contract
                # without creating a nested executor/event-loop lifecycle.
                return adapter.fetch(url)

        self.provider = Provider()
        self.limit = limit
        self.statistics = {}

    def fetch(self, url: str) -> dict:
        parts = urlparse(url)
        params = parse_qs(parts.query)
        if parts.path.endswith("/search"):
            params["maxResults"] = [str(self.limit)]
            params["order"] = ["viewCount"]
        if parts.path.endswith("/videos"):
            params["part"] = [str(params.get("part", [""])[0]) + ",statistics"]
        url = urlunparse(parts._replace(query=urlencode(params, doseq=True)))
        with urlopen(Request(url, headers={"User-Agent": "Osaka-CD-Metadata/8"}), timeout=15) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise MetadataSearchError("metadata_response_too_large")
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("error"):
            raise MetadataSearchError("youtube_api_response_error")
        for item in data.get("items", []):
            raw = (item.get("statistics") or {}).get("viewCount")
            if isinstance(raw, str) and raw.isdigit():
                self.statistics[str(item.get("id"))] = int(raw)
        return data

    async def search_async(self, query: str, *, seed_video_ids=()) -> list[dict]:
        found = await self.provider.youtube_search(query, limit=self.limit, include_details=True)
        for video_id in seed_video_ids:
            if VIDEO_ID.fullmatch(str(video_id)):
                item = await self.provider.youtube_video_metadata(video_id)
                if item:
                    found.append(item)
        result = []
        for item in found:
            video_id = str((parse_qs(urlparse(item.webpage_url).query).get("v") or [""])[0])
            result.append(_candidate({"video_id": video_id, "title": item.title, "channel": item.artist,
                                      "thumbnail": item.thumbnail, "duration": item.duration,
                                      "view_count": self.statistics.get(video_id)},
                                     source="youtube-player-api", query=query))
        return result

    def search(self, query: str, *, seed_video_ids=()) -> list[dict]:
        return asyncio.run(self.search_async(query, seed_video_ids=seed_video_ids))


def _yt_dlp_search(query: str, *, limit: int, seed_video_ids=(), candidate_filter=None) -> list[dict]:
    import yt_dlp
    # No cookies, account credentials, audio downloads, or extractor config.
    class SilentLogger:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass

    options = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": SilentLogger(),
               "extract_flat": True, "socket_timeout": 12, "retries": 1,
               "noplaylist": True, "ignoreerrors": False}
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as extractor:
        found = extractor.extract_info(f"ytsearch{limit}:{query}", download=False) or {}
    rows = [item for item in found.get("entries", []) if isinstance(item, dict)]
    ids = list(dict.fromkeys(str(item) for item in seed_video_ids if VIDEO_ID.fullmatch(str(item))))
    # Flat search statistics can be unavailable; resolve those entries directly.
    missing = [str(row.get("id")) for row in rows if type(row.get("view_count")) is not int
               and (candidate_filter is None or candidate_filter(row))
               and VIDEO_ID.fullmatch(str(row.get("id") or ""))]
    if ids or missing:
        options["extract_flat"] = False
        with yt_dlp.YoutubeDL(options) as extractor:
            for video_id in dict.fromkeys([*ids, *missing]):
                try:
                    detailed = extractor.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
                except Exception:
                    continue
                if isinstance(detailed, dict):
                    rows = [row for row in rows if row.get("id") != video_id]
                    rows.append(detailed)
    return [_candidate(row, source="yt-dlp-public-metadata", query=query) for row in rows]


def search_metadata(title: str, artist: str, aliases=(), limit: int = 25, *, artist_aliases=(),
                    candidates=None, search_backend=None, youtube_api_key=None, duration=None,
                    allow_versions=(), seed_video_ids=(), queries=()) -> dict:
    """Select the most viewed identified version among inspected candidates.

    ``limit`` is the number of results *per query*, bounded to YouTube's 50.
    Aliases and artist_aliases are explicit sidecar metadata. A candidate file is
    offline input, never portrayed as a live search. Custom backends receive
    ``(query, limit=..., seed_video_ids=...)`` and return public candidate dicts.
    This synchronous API belongs in a worker thread when called by async code.
    """
    normal, _, _ = player_modules()
    title, artist = normal.clean_metadata_title(title), str(artist or "").strip()
    if not title or not 1 <= int(limit) <= 50:
        raise ValueError("invalid_metadata_search_input")
    limit = int(limit)
    alternatives = normal.unique_queries(title, *aliases)
    artist_names = normal.unique_queries(artist, *artist_aliases)
    query_list = normal.unique_queries(*queries, *(f"{name} {alternate}".strip()
                                                   for alternate in alternatives
                                                   for name in (artist_names or [""])))[:16]
    checked_at = datetime.now(timezone.utc).isoformat()
    audit = {"checked_at": checked_at, "queries": [],
             "scope": "highest-view-count-among-inspected-identified-recordings",
             "global_youtube_maximum_claimed": False,
             "criteria": {"title_aliases": alternatives, "artist_aliases": artist_names,
                          "artist_evidence_required": True, "view_count_required": True,
                          "unrequested_recording_versions_rejected": True,
                          "named_version_labels_must_match": True,
                          "ambiguous_performer_attribution_rejected": True,
                          "unrequested_discussion_formats_rejected": True,
                          "title_token_equivalence": "english-final-ing-g-dropping-v1",
                          "requested_versions": list(allow_versions)}, "provider": "provided-candidates"}
    found = []
    if candidates is not None:
        if not isinstance(candidates, (list, tuple)) or len(candidates) > 5000:
            raise ValueError("invalid_metadata_candidates")
        found = [_candidate(row, source="provided-candidates") for row in candidates if isinstance(row, dict)]
        audit["provided_candidates"] = len(candidates)
    else:
        key = youtube_api_key or os.getenv("YOUTUBE_API_KEY") or os.getenv("GOOGLE_YOUTUBE_API_KEY")
        if search_backend is None and key:
            search_backend = YouTubePlayerAdapter(key, limit=limit).search
            provider_name = "youtube-player-api"
        elif search_backend is None:
            def public_search(query, *, limit, seed_video_ids):
                def plausible(row):
                    _, reasons = match_candidate(title, artist, _candidate(row, source="yt-dlp-public-metadata"),
                                                   aliases=aliases, artist_aliases=artist_aliases,
                                                   allow_versions=allow_versions, duration=duration)
                    return all(reason == "view_count_unavailable" for reason in reasons)
                return _yt_dlp_search(query, limit=limit, seed_video_ids=seed_video_ids, candidate_filter=plausible)
            search_backend = public_search
            provider_name = "yt-dlp-public-metadata"
        else:
            provider_name = "custom-player-backend"
        audit["provider"] = provider_name
        for query in query_list:
            row = {"query": query, "limit": limit, "returned": 0}
            try:
                if provider_name == "youtube-player-api":
                    result = search_backend(query, seed_video_ids=seed_video_ids)
                else:
                    result = search_backend(query, limit=limit, seed_video_ids=seed_video_ids)
                row["returned"] = len(result)
                found.extend(_candidate(item, source=provider_name, query=query) for item in result if isinstance(item, dict))
            except Exception as error:
                # HTTP errors can embed API keys in URLs. Retain the type only.
                row["error"] = type(error).__name__
            audit["queries"].append(row)
        if audit["queries"] and all(row.get("error") for row in audit["queries"]):
            raise MetadataSearchError("metadata_providers_failed", audit)
    deduplicated = {}
    for candidate in found:
        identifier = candidate["video_id"]
        previous = deduplicated.get(identifier)
        if previous is None or (type(candidate.get("view_count")) is int and type(previous.get("view_count")) is not int):
            deduplicated[identifier] = candidate
    inspected, accepted = [], []
    for candidate in deduplicated.values():
        candidate.setdefault("view_count_observed_at", checked_at if candidates is None else None)
        matches, reasons = match_candidate(title, artist, candidate, aliases=aliases,
                                           artist_aliases=artist_aliases, allow_versions=allow_versions,
                                           duration=duration)
        annotated = dict(candidate, matching=matches, rejection_reasons=reasons)
        inspected.append(annotated)
        if matches:
            accepted.append(candidate)
    audit.update(inspected_candidates=len(inspected), accepted_candidates=len(accepted),
                 rejected_candidates=len(inspected) - len(accepted), candidates=inspected)
    if not accepted:
        raise MetadataNoMatchError("no_identified_recording_with_view_count", audit)
    chosen = max(accepted, key=lambda row: (row["view_count"], row["video_id"]))
    chosen = dict(chosen, selection_basis=audit["scope"], checked_at=checked_at)
    audit["selected_video_id"] = chosen["video_id"]
    audit["partial_provider_failure"] = any(row.get("error") for row in audit["queries"])
    return {"selected": chosen, "audit": audit, "candidates": inspected}
