"""Catálogo de canto do bot; a escolha vira uma partitura antes de entrar na fila."""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from osaka_cd.runtime import data_root, pin_snapshot, register_validator

DEFAULT_MELODY_ID = "ttv-demo-v1"
_LOG = logging.getLogger(__name__)
_SCORE_FILE = Path(__file__).with_name("cd_song_scores.json")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FAILED_CATALOG_SIGNATURE: tuple[int, ...] | None = None
_LOOKUP_SNAPSHOT = None
_LOOKUP = {}
_SINGLE_VOWELS = frozenset("a á à â ã e é ê i í o ó ô õ u ú".split())
_SCORE_SNAPSHOTS = OrderedDict()
_SCORE_CACHE_BYTES = 0
_SCORE_CACHE_LIMIT = 8 * 1024 * 1024
_CATALOG_MUSIC_LIMIT = 32 * 1024 * 1024
_MUSIC_LOCK = threading.RLock()


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
    arrangement_json: str = ""
    arrangement_sha256: str = ""
    arrangement_version: int = 1
    arrangement_preview_json: str = ""
    arrangement_preview_sha256: str = ""
    arrangement_preview_performance_sha256: str = ""


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


def _score_path() -> Path:
    root = data_root(_SCORE_FILE.parent)
    return _SCORE_FILE if root == _SCORE_FILE.parent else root / "cd_song_scores.json"


def _harmonized_arrangement_fields(score, *, arrangement=None, source_sha256="",
                                  preview_start_ms=0, preview_end_ms=None):
    """Derive playback music after validating the immutable source catalog.

    Imported multipart music is handled by the original validation path. Mono
    sources receive a separate generated voice without rewriting their files,
    source notes or lead score. Effective hashes invalidate old mono samples.
    """
    from osaka_cd.arrangement import (arrangement_sha256,
        arrangement_performance_sha256, preview_arrangement)
    from osaka_cd.harmony import harmonize_monophonic
    if arrangement is None:
        arrangement = {'score_schema': 2, 'duration_ms': score['duration_ms'],
            'parts': [{'id': 'lead', 'role': 'lead', 'voice_id': 'kasane-teto',
                       'lyrics_source': 'message', 'notes': score['notes']}],
            'source': {'sha256': source_sha256} if source_sha256 else {}}
    effective = harmonize_monophonic(arrangement)
    preview = preview_arrangement(effective, start_ms=preview_start_ms,
        end_ms=preview_end_ms if preview_end_ms is not None else score['duration_ms'])['arrangement']
    return {
        'arrangement_json': json.dumps(effective, ensure_ascii=False, separators=(',', ':'), allow_nan=False),
        'arrangement_sha256': arrangement_sha256(effective),
        'arrangement_version': 2,
        'arrangement_preview_json': json.dumps(preview, ensure_ascii=False, separators=(',', ':'), allow_nan=False),
        'arrangement_preview_sha256': arrangement_sha256(preview),
        'arrangement_preview_performance_sha256': arrangement_performance_sha256(preview),
    }


def _read_song_melodies(path) -> tuple[Melody, ...]:
    entries = json.loads(Path(path or _score_path()).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("invalid_cd_catalog_size")
    seen = set()
    songs = []
    music_bytes = 0
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
        arrangement = entry.get("arrangement")
        arrangement_path = entry.get("arrangement_path")
        if arrangement_path is not None:
            # Sidecars are data under this same immutable catalog generation.
            # Resolve without permitting absolute paths, traversal or symlinks.
            if not isinstance(arrangement_path, str) or arrangement_path != f"cogs/tts/cd/arrangements/{identity}.json":
                raise ValueError("invalid_cd_arrangement_path")
            target = Path(path or _score_path()).parent / "cd/arrangements" / f"{identity}.json"
            if (target.is_symlink() or not target.resolve().is_relative_to(Path(path or _score_path()).parent.resolve())
                    or not 0 < target.stat().st_size <= 16 * 1024 * 1024):
                raise ValueError("invalid_cd_arrangement_file")
            arrangement = json.loads(target.read_text(encoding="utf-8"))
        arrangement_fields = {}
        preview_start, preview_end = 0, entry['duration_ms']
        if arrangement is not None:
            from osaka_cd.arrangement import (validate_arrangement, arrangement_sha256,
                arrangement_performance_sha256, preview_arrangement)
            arrangement = validate_arrangement(arrangement)
            if source_hash and arrangement.get("source", {}).get("sha256") != source_hash:
                raise ValueError("invalid_cd_arrangement_source_hash")
            arrangement_digest = arrangement_sha256(arrangement)
            if entry.get("arrangement_sha256") != arrangement_digest:
                raise ValueError("invalid_cd_arrangement_hash")
            preview_start = entry.get("arrangement_preview_start_ms", 0)
            preview_end = entry.get("arrangement_preview_end_ms", min(arrangement["duration_ms"], entry["duration_ms"]))
            if type(preview_start) is not int or type(preview_end) is not int:
                raise ValueError("invalid_cd_arrangement_preview_window")
            preview = preview_arrangement(arrangement, start_ms=preview_start, end_ms=preview_end)["arrangement"]
            lead = next(part for part in preview["parts"] if part["role"] == "lead")
            timing_keys = ("midi_note", "start_ms", "duration_ms", "velocity")
            lead_notes = [{key: note[key] for key in timing_keys} for note in lead["notes"]]
            visible_notes = [{key: note[key] for key in timing_keys} for note in score["notes"]]
            if preview["duration_ms"] != entry["duration_ms"] or lead_notes != visible_notes:
                raise ValueError("invalid_cd_arrangement_lead_score")
            preview_digest = arrangement_sha256(preview)
            if "preview_arrangement_sha256" in entry and entry["preview_arrangement_sha256"] != preview_digest:
                raise ValueError("invalid_cd_preview_arrangement_hash")
            preview_performance = arrangement_performance_sha256(preview)
            if ("preview_performance_sha256" in entry
                    and entry["preview_performance_sha256"] != preview_performance):
                raise ValueError("invalid_cd_preview_performance_hash")
            arrangement_fields = {
                "arrangement_json": json.dumps(arrangement, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                "arrangement_sha256": arrangement_digest,
                "arrangement_version": 2,
                "arrangement_preview_json": json.dumps(preview, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                "arrangement_preview_sha256": preview_digest,
                "arrangement_preview_performance_sha256": preview_performance,
            }
        if arrangement is None or len(arrangement['parts']) == 1:
            arrangement_fields = _harmonized_arrangement_fields(
                score, arrangement=arrangement, source_sha256=source_hash,
                preview_start_ms=preview_start, preview_end_ms=preview_end)
        melody = Melody(
            id=entry["id"], name=entry["name"],
            description=f"{entry['artist']} · {entry['excerpt']}",
            duration_ms=entry["duration_ms"], note_count=len(score["notes"]),
            score_json=json.dumps(score, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            revision=revision, artist=entry["artist"], source_url=entry["source_url"],
            source_sha256=source_hash, score_sha256=digest,
            **arrangement_fields,
        )
        music_bytes += sum(len(payload.encode('utf-8')) for payload in (
            melody.score_json, melody.arrangement_json, melody.arrangement_preview_json))
        if music_bytes > _CATALOG_MUSIC_LIMIT:
            raise ValueError("cd_catalog_music_memory_limit")
        songs.append(melody)
    return tuple(songs)


def _path_signature(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


def _song_melodies(path=None) -> tuple[Melody, ...]:
    """Reuse reader validation without retaining unbounded old generations."""
    path = Path(path or _score_path())
    with _MUSIC_LOCK:
        return _cached_song_melodies(path)


def _cached_song_melodies(path) -> tuple[Melody, ...]:
    global _SCORE_CACHE_BYTES
    previous = _SCORE_SNAPSHOTS.get(path)
    if previous is not None:
        dependencies, songs, size = previous
        if all(_path_signature(dependency) == signature for dependency, signature in dependencies):
            _SCORE_SNAPSHOTS.move_to_end(path)
            return songs
        _SCORE_SNAPSHOTS.pop(path)
        _SCORE_CACHE_BYTES -= size
    songs = _read_song_melodies(path)
    dependencies = [(path, _path_signature(path))]
    for melody in songs:
        if melody.arrangement_json:
            sidecar = path.parent / 'cd/arrangements' / f'{melody.id}.json'
            if sidecar.is_file():
                dependencies.append((sidecar, _path_signature(sidecar)))
    size = sum(len(payload.encode('utf-8')) for melody in songs for payload in (
        melody.score_json, melody.arrangement_json, melody.arrangement_preview_json))
    if size <= _SCORE_CACHE_LIMIT:
        _SCORE_SNAPSHOTS[path] = (dependencies, songs, size)
        _SCORE_CACHE_BYTES += size
        while len(_SCORE_SNAPSHOTS) > 8 or _SCORE_CACHE_BYTES > _SCORE_CACHE_LIMIT:
            _, (_, _, removed) = _SCORE_SNAPSHOTS.popitem(last=False)
            _SCORE_CACHE_BYTES -= removed
    return songs


_DEFAULT_MELODY = Melody(
    id=DEFAULT_MELODY_ID,
    name="Padrão",
    description="C4 · E4 · G4 · E4",
    duration_ms=4000,
    note_count=4,
)
_DEFAULT_MELODY = replace(_DEFAULT_MELODY, **_harmonized_arrangement_fields(
    json.loads(_DEFAULT_MELODY.score_json)))
try:
    _MELODIES = (_DEFAULT_MELODY, *_song_melodies())
except (OSError, ValueError, KeyError, TypeError) as exc:
    _LOG.warning("[cd_catalog] catálogo inicial recusado: %s", type(exc).__name__)
    _MELODIES = (_DEFAULT_MELODY,)


try:
    _STAT = _score_path().stat()
    _CATALOG_SIGNATURE = (str(_score_path()), _STAT.st_mtime_ns, _STAT.st_ctime_ns, _STAT.st_size, _STAT.st_ino)
except OSError:
    _CATALOG_SIGNATURE = (0, 0, 0, 0)

def list_melodies() -> tuple[Melody, ...]:
    path = _score_path()
    with _MUSIC_LOCK:
        return _list_melodies_current(path)


def _list_melodies_current(path) -> tuple[Melody, ...]:
    global _MELODIES, _CATALOG_SIGNATURE, _FAILED_CATALOG_SIGNATURE
    signature = (0, 0, 0, 0)
    try:
        stat = path.stat()
        signature = (str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        if signature != _CATALOG_SIGNATURE and signature != _FAILED_CATALOG_SIGNATURE:
            updated = (_MELODIES[0], *_song_melodies(path))
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
    path = _score_path()
    selected_root = path.parent
    paths = [path, path.with_name('cd_song_videos.json'),
             selected_root / 'cd' / 'previews' / 'index.json']
    roots = [selected_root / 'cd' / 'thumbnails', selected_root / 'cd' / 'previews']
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
    return get_melody(value).id


def get_melody(value: object = None) -> Melody:
    global _LOOKUP_SNAPSHOT, _LOOKUP
    if value is not None and not isinstance(value, str):
        raise ValueError("Escolha uma melodia disponível no painel de canto.")
    identity = str(value or "").strip() or DEFAULT_MELODY_ID
    snapshot = list_melodies()
    with _MUSIC_LOCK:
        if snapshot is not _LOOKUP_SNAPSHOT:
            _LOOKUP = {melody.id: melody for melody in snapshot}
            _LOOKUP_SNAPSHOT = snapshot
        melody = _LOOKUP.get(identity)
    if melody is None:
        raise ValueError("Essa melodia está indisponível. Escolha outra no painel de canto.")
    return melody


def score_for_melody(value: object = None) -> dict:
    # JSON keeps the catalog immutable; each queue request owns its score.
    return json.loads(get_melody(value).score_json)


def arrangement_for_melody(value: object = None, *, preview: bool = False) -> dict | None:
    """Each request owns the effective authored or automatically harmonized music."""
    identity = value.get("ttv_melody_id") if isinstance(value, dict) else value
    melody = get_melody(identity)
    payload = melody.arrangement_preview_json if preview else melody.arrangement_json
    return json.loads(payload) if payload else None


build_singing_arrangement = arrangement_for_melody


register_validator(lambda root: _song_melodies(root / "cogs/tts/cd_song_scores.json"))


def fit_catalog_lyrics_to_score(text: str, segments: list[dict] | None, score: dict
                                ) -> tuple[str, list[dict] | None]:
    """Keep words and effects intact for the automatic execution-score adapter.

    The fixed catalog remains unchanged. Its note count is no longer a minimum
    or maximum lyric count; osaka_cd.lyrics produces legal physical requests.
    """
    return text, [dict(part) for part in segments] if segments is not None else None


def fit_arrangement_lyrics(text: str, segments: list[dict] | None, arrangement: dict
                           ) -> tuple[str, list[dict] | None]:
    """Keep user lyrics intact for the lead of the selected execution template.

    Harmony lyrics remain the arrangement's explicit MIDI or vowel guides.
    Written words are left to the real voicebank phonemizer, as in v1.
    """
    parts = arrangement.get("parts") if isinstance(arrangement, dict) else None
    leads = [part for part in parts if isinstance(part, dict) and part.get("role") == "lead"] if isinstance(parts, list) else []
    if len(leads) != 1 or not isinstance(leads[0].get("notes"), list) or not leads[0]["notes"]:
        raise ValueError("O arranjo precisa de uma melodia principal válida.")
    return fit_catalog_lyrics_to_score(text, segments, {"notes": leads[0]["notes"]})


def minimum_singing_syllables(score: dict | None) -> int:
    """Exact lower bound for 1.11.41's allowed contiguous vowel spans.

    The worker permits at most 18 seconds and 40 semitones per vowel, and a
    pause over 20 ms requires a fresh attack. Greedy maximal spans minimize
    their count because every constraint is monotonic as notes are appended.
    This does not estimate the syllables of arbitrary written lyrics.
    """
    notes = score.get("notes") if isinstance(score, dict) else None
    if not isinstance(notes, list) or not notes:
        return 0
    count = 1
    first, previous = notes[0], notes[0]
    low = high = first["midi_note"]
    for note in notes[1:]:
        pitch = note["midi_note"]
        end = note["start_ms"] + note["duration_ms"]
        if (note["start_ms"] - previous["start_ms"] - previous["duration_ms"] > 20
                or end - first["start_ms"] > 18000
                or max(high, pitch) - min(low, pitch) > 40):
            count += 1
            first = note
            low = high = pitch
        else:
            low, high = min(low, pitch), max(high, pitch)
        previous = note
    return count


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def format_score_notes(melody: Melody) -> str:
    """Represent the *actual* MIDI score, in its stored order and octave."""
    return _score_notes(melody.score_json)


@lru_cache(maxsize=256)
def _score_notes(score_json: str) -> str:
    score = json.loads(score_json)
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
