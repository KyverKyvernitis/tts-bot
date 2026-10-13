"""Catálogo de canto do bot; a escolha vira uma partitura antes de entrar na fila."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re



DEFAULT_MELODY_ID = "ttv-demo-v1"
_LOG = logging.getLogger(__name__)
_SCORE_FILE = Path(__file__).with_name("cd_song_scores.json")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FAILED_CATALOG_SIGNATURE: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class Melody:
    id: str
    name: str
    description: str
    duration_ms: int
    note_count: int
    thumbnail_url: str = ""
    score_json: str = (
        '{"version":1,"track_index":0,"duration_ms":4000,"lyrics_source":"message",'
        '"notes":[{"midi_note":60,"start_ms":0,"duration_ms":1000,"velocity":100},'
        '{"midi_note":64,"start_ms":1000,"duration_ms":1000,"velocity":100},'
        '{"midi_note":67,"start_ms":2000,"duration_ms":1000,"velocity":100},'
        '{"midi_note":64,"start_ms":3000,"duration_ms":1000,"velocity":100}]}'
    )
    revision: int = 1
    artist: str = ""
    source_url: str = ""
    source_sha256: str = ""
    score_sha256: str = ""


def catalog_score(entry: dict) -> dict:
    """The exact singing payload, independent of cover and MIDI provenance."""
    return {"version": 1, "track_index": 0, "duration_ms": entry["duration_ms"],
            "lyrics_source": "message", "notes": entry["notes"]}


def score_sha256(score: dict) -> str:
    """Canonical v2 preview identity. MIDI source hashes are a different key."""
    raw = json.dumps(score, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_score(entry: dict) -> dict:
    notes = entry["notes"]
    duration = entry["duration_ms"]
    if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
        raise ValueError("invalid_cd_score_notes")
    if type(duration) is not int or not 1 <= duration <= 60_000:
        raise ValueError("invalid_cd_score_duration")
    previous_end = 0
    for note in notes:
        if not isinstance(note, dict):
            raise ValueError("invalid_cd_score_note")
        for key, minimum, maximum in (("midi_note", 36, 96), ("start_ms", 0, duration),
                                      ("duration_ms", 40, duration), ("velocity", 1, 127)):
            value = note[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid_cd_score_note")
        if note["start_ms"] < previous_end:
            raise ValueError("invalid_cd_score_overlap")
        previous_end = note["start_ms"] + note["duration_ms"]
        if previous_end > duration:
            raise ValueError("invalid_cd_score_end")
        if "lyric" in note and (not isinstance(note["lyric"], str) or len(note["lyric"]) > 128):
            raise ValueError("invalid_cd_score_lyric")
    return catalog_score(entry)


def _song_melodies() -> tuple[Melody, ...]:
    entries = json.loads(_SCORE_FILE.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("invalid_cd_catalog_size")
    seen = set()
    songs = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid_cd_catalog_entry")
        identity = entry["id"]
        if (not isinstance(identity, str) or not identity or len(identity) > 90
                or not _ID.fullmatch(identity)
                or identity in seen or identity == DEFAULT_MELODY_ID):
            raise ValueError("invalid_cd_catalog_id")
        seen.add(identity)
        for key, limit in (("name", 100), ("artist", 200), ("excerpt", 500), ("source_url", 2048)):
            if not isinstance(entry[key], str) or len(entry[key]) > limit or (key == "name" and not entry[key].strip()):
                raise ValueError("invalid_cd_catalog_metadata")
        revision = entry.get("revision", 1)
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid_cd_catalog_revision")
        source_hash = entry.get("source_sha256", "")
        if not isinstance(source_hash, str) or (source_hash and not _SHA256.fullmatch(source_hash)):
            raise ValueError("invalid_cd_source_hash")
        score = _validate_score(entry)
        digest = score_sha256(score)
        if "score_sha256" in entry and entry["score_sha256"] != digest:
            raise ValueError("invalid_cd_score_hash")
        songs.append(Melody(
            id=entry["id"], name=entry["name"],
            description=f"{entry['artist']} · {entry['excerpt']}",
            duration_ms=entry["duration_ms"], note_count=len(score["notes"]),
            score_json=json.dumps(score, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            revision=revision, artist=entry["artist"], source_url=entry["source_url"],
            source_sha256=source_hash, score_sha256=digest,
        ))
    return tuple(songs)


_DEFAULT_MELODY = Melody(
    id=DEFAULT_MELODY_ID,
    name="Padrão",
    description="C4 · E4 · G4 · E4",
    duration_ms=4000,
    note_count=4,
)
try:
    _MELODIES = (_DEFAULT_MELODY, *_song_melodies())
except (OSError, ValueError, KeyError, TypeError) as exc:
    _LOG.warning("[cd_catalog] catálogo inicial recusado: %s", type(exc).__name__)
    _MELODIES = (_DEFAULT_MELODY,)


try:
    _STAT = _SCORE_FILE.stat()
    _CATALOG_SIGNATURE = (_STAT.st_mtime_ns, _STAT.st_ctime_ns, _STAT.st_size, _STAT.st_ino)
except OSError:
    _CATALOG_SIGNATURE = (0, 0, 0, 0)

def list_melodies() -> tuple[Melody, ...]:
    global _MELODIES, _CATALOG_SIGNATURE, _FAILED_CATALOG_SIGNATURE
    signature = (0, 0, 0, 0)
    try:
        stat = _SCORE_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        if signature != _CATALOG_SIGNATURE and signature != _FAILED_CATALOG_SIGNATURE:
            updated = (_MELODIES[0], *_song_melodies())
            _MELODIES = updated
            _CATALOG_SIGNATURE = signature
            _FAILED_CATALOG_SIGNATURE = None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        # Continue serving the previous complete catalog during an interrupted
        # publication. Never replace a valid snapshot with half-written data.
        if _FAILED_CATALOG_SIGNATURE != signature:
            _LOG.warning("[cd_catalog] recarga recusada: %s", type(exc).__name__)
        _FAILED_CATALOG_SIGNATURE = signature
    return _MELODIES


def catalog_signature() -> tuple[int, ...]:
    """Detect artifacts arriving after their manifests, including cover rewrites."""
    result = []
    data_root = _SCORE_FILE.parent
    paths = [_SCORE_FILE, _SCORE_FILE.with_name('cd_song_videos.json'),
             data_root / 'cd' / 'previews' / 'index.json']
    roots = [data_root / 'cd' / 'thumbnails', data_root / 'cd' / 'previews']
    configured = os.getenv('TTV_CD_THUMBNAIL_CACHE_DIR', '').strip()
    if configured:
        roots.append(Path(configured).expanduser())
    for root in roots:
        paths.append(root)
        try:
            paths.extend(sorted(path for path in root.iterdir() if path.suffix in {'.jpg', '.mp3'}))
        except OSError:
            continue
    for path in paths:
        result.append(int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], 'big'))
        try:
            stat = path.stat()
            result.extend((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
        except OSError:
            result.extend((0, 0, 0, 0))
    return tuple(result)


def normalize_melody_id(value: object = None) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("Escolha uma melodia disponível no painel de canto.")
    identity = str(value or "").strip() or DEFAULT_MELODY_ID
    if not any(melody.id == identity for melody in list_melodies()):
        raise ValueError("Essa melodia está indisponível. Escolha outra no painel de canto.")
    return identity


def get_melody(value: object = None) -> Melody:
    identity = normalize_melody_id(value)
    return next(melody for melody in list_melodies() if melody.id == identity)


def score_for_melody(value: object = None) -> dict:
    # JSON keeps the catalog immutable; each queue request owns its score.
    return json.loads(get_melody(value).score_json)


def fit_catalog_lyrics_to_score(text: str, segments: list[dict] | None, score: dict
                                ) -> tuple[str, list[dict] | None]:
    """Keep the lyric singable when shorthand '~' counts differ from a catalog score.

    A curated catalog score can receive freeform short lyrics: excess
    continuation marks are dropped from the right; when
    underspecified, automatic syllable alignment decides where to sustain.
    """
    if not segments or not any(part.get("text") == "~" and part.get("hold") for part in segments):
        return text, segments
    count = len(score["notes"])
    slots = sum(part.get("text") != "|" for part in segments)
    if slots == count:
        return text, segments
    result = [dict(part) for part in segments]
    if slots > count:
        excess = slots - count
        for index in range(len(result) - 1, -1, -1):
            if excess <= 0:
                break
            part = result[index]
            if part.get("text") == "~" and part.get("hold"):
                result.pop(index)
                excess -= 1
        if excess:
            # A lyric has more syllables than notes. Do not silently erase text.
            raise ValueError("A letra tem mais sílabas que notas nesta melodia.")
        slots = count
    if slots < count:
        # A shorter lyric can use the renderer's balanced automatic alignment.
        result = [part for part in result if not (part.get("text") == "~" and part.get("hold"))]
    clean = " ".join(part["text"] for part in result)
    if not any(part.get("hold") or part.get("fade_in") or part.get("fade_out")
               or part.get("pause_after_ms") or part["text"] == "|" for part in result):
        return clean, None
    return clean, result


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def format_score_notes(melody: Melody) -> str:
    """Represent the *actual* MIDI score, in its stored order and octave."""
    score = json.loads(melody.score_json)
    sequence = []
    for note in score["notes"]:
        midi = note["midi_note"]
        if type(midi) is not int or not 0 <= midi <= 127:
            raise ValueError("invalid_score_midi_note")
        sequence.append(f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}")
    return " · ".join(sequence)


def format_score_rhythm(melody: Melody) -> str:
    """Durations follow the note line; silence is explicit, including its tail."""
    score = json.loads(melody.score_json)
    sequence = []
    cursor = 0

    def seconds(duration: int) -> str:
        return f"{duration / 1000:g}".replace(".", ",") + "s"

    for note in score["notes"]:
        if note["start_ms"] > cursor:
            sequence.append("𝄽" + seconds(note["start_ms"] - cursor))
        sequence.append(seconds(note["duration_ms"]))
        cursor = note["start_ms"] + note["duration_ms"]
    if score["duration_ms"] > cursor:
        sequence.append("𝄽" + seconds(score["duration_ms"] - cursor))
    return " · ".join(sequence)

def preview_text_for_melody(value: object = None, language: str = "pt-BR") -> str:
    """Keep one fresh vowel attack per note, including after musical rests."""
    melody = get_melody(value)
    if melody.id == DEFAULT_MELODY_ID:
        return "My dad has bread" if language == "en-US" else "Meu pai tem pão"
    syllable = "la" if language == "en-US" else "lá"
    return " ".join([syllable] * melody.note_count)
