"""Discover a song's artist from public identity evidence, before CD import.

This does not select or download artwork. The normal CD metadata matcher and
cover importer remain responsible for the recording and its image. A channel
name alone is deliberately not treated as a composer's name.
"""
from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


_MAX_RESPONSE = 1024 * 1024
_MAX_RESULTS = 12
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_SEPARATOR = re.compile(r'\s+(?:-|–|—|\|)\s+')
_VOICES = frozenset({'Vocaloid', 'UTAU', 'SynthesizerV', 'CeVIO', 'VoiceSynthesizer',
                     'VoiSona', 'ACEVirtualSinger', 'OtherVoiceSynthesizer'})


class TitleMetadataError(ValueError):
    def __init__(self, code='metadata_unavailable'):
        self.code = code if code in {'metadata_ambiguous', 'metadata_unavailable', 'metadata_invalid'} else 'metadata_unavailable'
        super().__init__(self.code)


def _metadata():
    # The caller executes this module in the existing importer's subprocess.
    import cd_metadata
    return cd_metadata


def _clean(value, limit=160):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKC', html.unescape(value)).strip()
    if not value or len(value) > limit or any(unicodedata.category(c).startswith('C') for c in value):
        return ''
    return value


def _unique(values, limit=12):
    result, seen = [], set()
    for raw in values:
        value = _clean(raw)
        key = _metadata().unicode_key(value)
        if value and key and key not in seen:
            result.append(value)
            seen.add(key)
    return result[:limit]


def _rows(value, limit):
    return value[:limit] if isinstance(value, (list, tuple)) else []


def _artist(value):
    value = _clean(value)
    if re.search(r'https?://|www\.', value, flags=re.IGNORECASE):
        return ''
    value = re.sub(r'[\[(【]\s*(?:official(?:\s+(?:music|lyric))?\s*(?:audio|video)?|'
                   r'music\s+video|audio\s+only|lyrics?|mv|hd|hq|4k)\s*[\])】]',
                   '', value, flags=re.IGNORECASE).strip()
    value = _clean(_metadata().player_modules()[0].clean_metadata_title(value))
    if re.search(r'^(?:feat\.?|ft\.?|vocals?|voice|vo\.?|sung\s+by|singer|歌唱|歌\s*[:：])\b',
                 value, flags=re.IGNORECASE):
        return ''
    if re.search(r'(?:SV|Synthesizer\s*V|Vocaloid|UTAU)\s*$', value, flags=re.IGNORECASE):
        return ''
    # Named synthesized singers are performer credits, not producer evidence.
    if _metadata().unicode_key(value) in {
            '初音ミク', '重音テト', '鏡音リン', '鏡音レン', '巡音ルカ', '歌愛ユキ',
            'hatsune miku', 'kasane teto', 'kaai yuki', 'kagamine rin',
            'kagamine len', 'megurine luka'}:
        return ''
    # A suffix like "Song - Original Song" is presentation, not an artist.
    words = set(_metadata().unicode_key(value).split())
    if words and words <= {'original', 'full', 'song', 'theme', 'official', 'release',
                           'audio', 'only', 'music', 'video', 'mv', 'lyrics', 'lyric',
                           'hd', 'hq', '4k', 'version', 'extended', 'edit', 'topic',
                           'visualizer', 'visualiser', 'letra', 'oficial', 'videoclipe'}:
        return ''
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _vocadb_search(title):
    # Fixed provider URL, no cookies, credentials, or arbitrary remote URLs.
    query = urlencode({'query': title, 'maxResults': _MAX_RESULTS,
        'fields': 'Names,Artists,PVs', 'nameMatchMode': 'Auto', 'lang': 'English'})
    request = Request('https://vocadb.net/api/songs?' + query,
        headers={'User-Agent': 'Osaka-CD-Title-Discovery/1', 'Accept': 'application/json'})
    with build_opener(_NoRedirect()).open(request, timeout=8) as response:
        data = response.read(_MAX_RESPONSE + 1)
    if len(data) > _MAX_RESPONSE:
        raise TitleMetadataError()
    payload = json.loads(data)
    rows = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_RESULTS:
        raise TitleMetadataError()
    return rows


def _name_match(title, names):
    wanted = _metadata().unicode_key(title)
    scores = []
    for name in names:
        found = _metadata().unicode_key(name)
        if found == wanted:
            scores.append(2)
        # A romanized suffix may be omitted in a filename. This is only used
        # with an original-song catalog entry and a unique producer identity.
        elif len(wanted) >= 6 and found.startswith(wanted) and len(found) - len(wanted) <= 2:
            scores.append(1)
    return max(scores, default=0)


def _catalog_identity(title, rows):
    matches = []
    for row in rows[:_MAX_RESULTS]:
        if not isinstance(row, dict) or row.get('songType') != 'Original':
            continue
        names = _unique([row.get('name'), *(entry.get('value') for entry in _rows(row.get('names'), 32)
                         if isinstance(entry, dict))])
        quality = _name_match(title, names)
        if not quality:
            continue
        producers, producer_names = [], []
        for credit in _rows(row.get('artists'), 32):
            if not isinstance(credit, dict) or not isinstance(credit.get('artist'), dict):
                continue
            artist = credit['artist']
            roles = set(str(credit.get('roles') or '').replace(' ', '').split(','))
            kind = artist.get('artistType')
            if kind in _VOICES or not (kind == 'Producer' or 'Composer' in roles):
                continue
            name = _artist(artist.get('name') or credit.get('name'))
            if not name:
                continue
            producers.append(str(artist.get('id') or _metadata().unicode_key(name)))
            producer_names.extend([name, credit.get('name'), *(entry.get('value') for entry in _rows(artist.get('names'), 32)
                                     if isinstance(entry, dict))])
        artists = _unique(producer_names)
        if not producers or not artists:
            continue
        seeds = []
        for pv in _rows(row.get('pvs'), 40):
            if (isinstance(pv, dict) and pv.get('service') == 'Youtube'
                    and pv.get('pvType') == 'Original' and not pv.get('disabled')
                    and _VIDEO.fullmatch(str(pv.get('pvId') or ''))):
                seeds.append(pv['pvId'])
        # Require an original recording link, not just an unverified name hit.
        if not seeds:
            continue
        matches.append((quality, tuple(sorted(set(producers))), {
            'title': title, 'artist': artists[0],
            'aliases': [name for name in names if _metadata().unicode_key(name) != _metadata().unicode_key(title)],
            'artist_aliases': artists[1:], 'seed_video_ids': list(dict.fromkeys(seeds))[:12],
            'search_query': f'{title} {artists[0]}'[:320],
            'selection_audit': {'provider': 'vocadb-public-song-identities',
                'song_id': row.get('id') if type(row.get('id')) is int else None,
                'title_match': 'exact_alias' if quality == 2 else 'romanized_prefix',
                'producer_ids': sorted(set(producers)), 'original_recording_links': len(seeds),
                'recording_still_requires_existing_importer_validation': True}}))
    if not matches:
        return None
    quality = max(row[0] for row in matches)
    best = [row for row in matches if row[0] == quality]
    if len({row[1] for row in best}) != 1:
        raise TitleMetadataError('metadata_ambiguous')
    return best[0][2]


def _title_artist(title, candidate):
    """Return identity evidence, never an arbitrary uploader channel."""
    module = _metadata()
    raw_title = _clean(candidate.get('title'), 500)
    structured = _artist(candidate.get('artist'))
    track = _clean(candidate.get('track'), 100)
    if structured and track and module.unicode_key(track) == module.unicode_key(title):
        return structured, 'structured_artist_and_track'
    normal = module.player_modules()[0]
    parts = _SEPARATOR.split(raw_title)
    for index, part in enumerate(parts):
        if module._contains(title, part, normal.normalize_text, title_contractions=True):
            other = parts[0] if index else (parts[1] if len(parts) > 1 else '')
            artist = _artist(other)
            if artist and not module._contains(title, artist, normal.normalize_text, title_contractions=True):
                return artist, 'recording_title_credit'
    # Common Japanese presentation: producer『song』 / producer「song」.
    quoted = re.fullmatch(r'(.{1,160}?)[『「](.+?)[』」].*', raw_title)
    if quoted and module._contains(title, quoted.group(2), normal.normalize_text, title_contractions=True):
        return _artist(quoted.group(1)), 'quoted_recording_title_credit'
    channel = _clean(candidate.get('channel') or candidate.get('uploader'))
    topic = re.fullmatch(r'(.{1,160}?)\s+-\s+Topic', channel, flags=re.IGNORECASE)
    if topic:
        return _artist(topic.group(1)), 'artist_topic_channel'
    if candidate.get('channel_is_verified') is True and channel:
        # A verified channel still needs explicit official-artist labeling.
        official = re.fullmatch(r'(.{1,160}?)\s*(?:[-–—]\s*)?(?:Official(?:\s+(?:Artist|Music))?\s+Channel|\(Official\))', channel, flags=re.IGNORECASE)
        if official:
            return _artist(official.group(1)), 'verified_official_artist_channel'
    return None, None


def _youtube_identity(title, rows):
    module = _metadata()
    groups = {}
    for raw in rows[:50]:
        if not isinstance(raw, dict):
            continue
        artist, evidence = _title_artist(title, raw)
        if not artist:
            continue
        candidate = module._candidate(raw, source='title-discovery-candidate')
        # Structured artist/track fields are explicit source metadata. Supply
        # that evidence to the existing matcher without rewriting the uploader.
        verified = dict(candidate)
        if evidence == 'structured_artist_and_track':
            verified['channel'] = artist
        valid, reasons = module.match_candidate(title, artist, verified)
        if not valid:
            continue
        key = module.unicode_key(artist)
        groups.setdefault(key, []).append((candidate, artist, evidence))
    if not groups:
        return None
    if len(groups) != 1:
        raise TitleMetadataError('metadata_ambiguous')
    candidates = next(iter(groups.values()))
    selected, artist, evidence = max(candidates, key=lambda row: (row[0]['view_count'], row[0]['video_id']))
    return {'title': title, 'artist': artist, 'aliases': [], 'artist_aliases': [],
        'seed_video_ids': [selected['video_id']], 'search_query': f'{title} {artist}'[:320],
        'selection_audit': {'provider': 'existing-youtube-metadata', 'evidence': evidence,
            'selected_video_id': selected['video_id'], 'inspected_candidates': min(50, len(rows)),
            'identified_artist_groups': 1, 'view_count': selected['view_count'],
            'recording_still_requires_existing_importer_validation': True}}


def resolve_title_metadata(title, *, candidates=None, search_backend=None,
                           vocadb_candidates=None, youtube_api_key=None):
    """Find an unambiguous public song identity using only its title.

    Explicit candidate arguments are offline provider seams. They never cause
    network traffic. Search is bounded to one catalog request and one YouTube
    query; the existing importer subsequently verifies the chosen recording.
    """
    title = _clean(title, 100)
    if not title:
        raise TitleMetadataError('metadata_invalid')
    if candidates is not None and (not isinstance(candidates, (list, tuple)) or len(candidates) > 50):
        raise TitleMetadataError('metadata_invalid')
    if vocadb_candidates is not None and (not isinstance(vocadb_candidates, (list, tuple)) or len(vocadb_candidates) > _MAX_RESULTS):
        raise TitleMetadataError('metadata_invalid')
    offline = candidates is not None or vocadb_candidates is not None or search_backend is not None
    catalog = vocadb_candidates
    if catalog is None and not offline:
        try:
            catalog = _vocadb_search(title)
        except Exception:
            catalog = []
    if catalog:
        identity = _catalog_identity(title, list(catalog))
        if identity:
            return identity
    rows = candidates
    if rows is None:
        if vocadb_candidates is not None and search_backend is None:
            rows = []
        else:
            module = _metadata()
            try:
                if search_backend is not None:
                    rows = search_backend(title, limit=25, seed_video_ids=())
                elif youtube_api_key or os.getenv('YOUTUBE_API_KEY') or os.getenv('GOOGLE_YOUTUBE_API_KEY'):
                    key = youtube_api_key or os.getenv('YOUTUBE_API_KEY') or os.getenv('GOOGLE_YOUTUBE_API_KEY')
                    rows = module.YouTubePlayerAdapter(key, limit=25).search(title)
                else:
                    def plausible(raw):
                        artist, evidence = _title_artist(title, raw)
                        if not artist:
                            return False
                        candidate = module._candidate(raw, source='title-discovery-candidate')
                        if evidence == 'structured_artist_and_track':
                            candidate['channel'] = artist
                        _, reasons = module.match_candidate(title, artist, candidate)
                        return all(reason == 'view_count_unavailable' for reason in reasons)
                    rows = module._yt_dlp_search(title, limit=25, candidate_filter=plausible)
            except Exception:
                raise TitleMetadataError() from None
    if not isinstance(rows, (list, tuple)) or len(rows) > 50:
        raise TitleMetadataError()
    identity = _youtube_identity(title, rows)
    if identity:
        return identity
    raise TitleMetadataError()
