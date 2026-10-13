"""Shared, offline CD overlay validation for Termux and the transactional updater.

ZIP paths are relative to the repository root. CD JSON files are increments by song
id, never replacement catalogues. This module has no bot/runtime imports.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

SCORES = "cogs/tts/cd_song_scores.json"
VIDEOS = "cogs/tts/cd_song_videos.json"
PREVIEWS = "cogs/tts/cd/previews/index.json"
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class CDPackageError(ValueError):
    pass


def _json(path: Path, default):
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file():
        raise CDPackageError(f"arquivo JSON inválido: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CDPackageError(f"JSON inválido: {path.name}") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def score_sha256(entry: dict) -> str:
    score = {"version": 1, "track_index": 0, "duration_ms": entry["duration_ms"],
             "lyrics_source": "message", "notes": entry["notes"]}
    return _digest(json.dumps(score, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CDPackageError(f"{name} inválido")
    return value


def _identity(value) -> str:
    if not isinstance(value, str) or len(value) > 90 or not _ID.fullmatch(value):
        raise CDPackageError("id de CD inválido")
    return value


def _scores(root: Path) -> dict[str, dict]:
    raw = _json(root / SCORES, [])
    if not isinstance(raw, list) or len(raw) > 500:
        raise CDPackageError("catálogo de partituras inválido")
    result = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise CDPackageError("partitura precisa ser um objeto")
        identity = _identity(entry.get("id"))
        if identity in result:
            raise CDPackageError(f"id duplicado: {identity}")
        for field in ("name", "artist", "excerpt", "source_url"):
            value = entry.get(field)
            if not isinstance(value, str) or len(value) > 4096 or (field in {"name", "artist"} and not value.strip()):
                raise CDPackageError(f"{field} inválido: {identity}")
        if not isinstance(entry.get("source_sha256"), str) or not _HASH.fullmatch(entry["source_sha256"]):
            raise CDPackageError(f"source_sha256 inválido: {identity}")
        duration = _integer(entry.get("duration_ms"), f"duração {identity}", 1, 60000)
        _integer(entry.get("revision", 1), f"revision {identity}", 1, 2147483647)
        notes = entry.get("notes")
        if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
            raise CDPackageError(f"notas inválidas: {identity}")
        end = 0
        for note in notes:
            if not isinstance(note, dict):
                raise CDPackageError(f"nota inválida: {identity}")
            _integer(note.get("midi_note"), "midi_note", 36, 96)
            start = _integer(note.get("start_ms"), "start_ms", 0, duration)
            length = _integer(note.get("duration_ms"), "duration_ms", 40, duration)
            _integer(note.get("velocity", 100), "velocity", 1, 127)
            if start < end or start + length > duration:
                raise CDPackageError(f"notas sobrepostas ou fora da duração: {identity}")
            end = start + length
        expected = entry.get("score_sha256")
        if expected is not None and expected != score_sha256(entry):
            raise CDPackageError(f"score_sha256 divergente: {identity}")
        result[identity] = entry
    return result


def _map(root: Path, rel: str, key: str | None = None) -> tuple[dict, dict]:
    raw = _json(root / rel, {})
    if not isinstance(raw, dict):
        raise CDPackageError(f"manifesto inválido: {rel}")
    items = raw.get(key, {}) if key else raw
    if not isinstance(items, dict):
        raise CDPackageError(f"mapa inválido: {rel}")
    for identity, item in items.items():
        _identity(identity)
        if not isinstance(item, dict):
            raise CDPackageError(f"registro inválido: {identity}")
    return raw, items


def _file(root: Path, rel: str, expected: str, limit: int) -> bytes:
    path = root / rel
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise CDPackageError(f"SHA-256 ausente/inválido: {rel}")
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise CDPackageError(f"artefato ausente/inválido: {rel}")
    data = path.read_bytes()
    if _digest(data) != expected:
        raise CDPackageError(f"hash divergente: {rel}")
    return data


def _jpeg(data: bytes, identity: str) -> None:
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise CDPackageError(f"JPEG inválido: {identity}")
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != "JPEG" or not (0 < picture.width <= 768 and 0 < picture.height <= 768):
                raise CDPackageError(f"dimensões da capa inválidas: {identity}")
            picture.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise CDPackageError(f"JPEG não decodifica: {identity}") from exc


def _audio(data: bytes, path: Path, identity: str, probe: bool) -> dict:
    if not (data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224)):
        raise CDPackageError(f"MP3 inválido: {identity}")
    if not probe:
        return {"verified": "signature-and-sha256"}
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise CDPackageError("ffprobe/ffmpeg necessários para validar áudio decodificado")
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                             "stream=codec_name:format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=30)
    try:
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        if result.returncode or info["streams"][0]["codec_name"] != "mp3" or not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid_audio")
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise CDPackageError(f"ffprobe recusou MP3: {identity}") from exc
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                             capture_output=True, timeout=45)
    if decoded.returncode:
        raise CDPackageError(f"MP3 não decodifica completamente: {identity}")
    return {"verified": "decoded", "duration_seconds": duration}


def validate_cd_tree(root: Path | str, ids=None, *, require_complete: bool = False,
                     audio_probe: bool = False) -> dict:
    root = Path(root)
    scores = _scores(root)
    _, videos = _map(root, VIDEOS, "songs")
    _, previews = _map(root, PREVIEWS)
    selected = set(scores) | set(videos) | set(previews) if ids is None else set(ids)
    artifacts = {}
    for identity in sorted(selected):
        _identity(identity)
        score = scores.get(identity)
        video = videos.get(identity)
        preview = previews.get(identity)
        status = {"score": "ready" if score else "pending", "thumbnail": "pending", "sample": "pending"}
        if video:
            if "id" in video and video["id"] != identity:
                raise CDPackageError(f"id dos metadados de vídeo divergente: {identity}")
            if not isinstance(video.get("video_id"), str) or not _VIDEO.fullmatch(video["video_id"]):
                raise CDPackageError(f"video_id inválido: {identity}")
            cover = f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg"
            if video.get("thumbnail_sha256") or (root / cover).exists():
                _jpeg(_file(root, cover, video.get("thumbnail_sha256"), 2 * 1024 * 1024), identity)
                status["thumbnail"] = "ready"
        if preview:
            if not score:
                raise CDPackageError(f"amostra sem partitura: {identity}")
            if preview.get("manifest_version") != 2:
                raise CDPackageError(f"manifesto de amostra legado; migração explícita necessária: {identity}")
            if preview.get("score_sha256") != score_sha256(score):
                raise CDPackageError(f"amostra pertence a outra partitura: {identity}")
            for key, value in (("id", identity), ("revision", score.get("revision", 1)),
                               ("source_sha256", score.get("source_sha256"))):
                if key in preview and preview[key] != value:
                    raise CDPackageError(f"{key} da amostra divergente: {identity}")
            if (preview.get("voice_id") != "kasane-teto" or preview.get("language") != "pt-BR"
                    or preview.get("pitch_semitones") != 0 or preview.get("speech_rate") != 1.0):
                raise CDPackageError(f"configuração da amostra divergente: {identity}")
            rel = f"cogs/tts/cd/previews/{identity}.mp3"
            data = _file(root, rel, preview.get("mp3_sha256"), 20 * 1024 * 1024)
            status["audio"] = _audio(data, root / rel, identity, audio_probe)
            status["sample"] = "ready"
        package_path = root / f"cogs/tts/cd/packages/{identity}.json"
        if package_path.exists():
            package = _json(package_path, {})
            if (not isinstance(package, dict) or package.get("manifest_version") != 1
                    or package.get("id") != identity or not score
                    or package.get("revision") != score.get("revision", 1)
                    or package.get("score_sha256") != score_sha256(score)
                    or package.get("source_sha256") != score.get("source_sha256")):
                raise CDPackageError(f"manifesto de pacote divergente: {identity}")
            declared = package.get("artifacts")
            if not isinstance(declared, dict):
                raise CDPackageError(f"artifacts inválido: {identity}")
            for artifact, item in declared.items():
                if artifact not in {"score", "thumbnail", "sample"}:
                    raise CDPackageError(f"tipo de artefato inválido: {identity}/{artifact}")
                if not isinstance(item, dict) or item.get("status") not in {"ready", "pending"}:
                    raise CDPackageError(f"estado de artefato inválido: {identity}/{artifact}")
                if item["status"] == "ready":
                    if artifact == "score":
                        if item.get("sha256") != score_sha256(score):
                            raise CDPackageError(f"hash da partitura no pacote divergente: {identity}")
                        continue
                    expected_path = (f"cogs/tts/cd/previews/{identity}.mp3" if artifact == "sample"
                                     else f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg" if video else "")
                    expected_hash = preview.get("mp3_sha256") if artifact == "sample" and preview else video.get("thumbnail_sha256") if video else None
                    if (status[artifact] != "ready" or item.get("path") != expected_path
                            or item.get("sha256") != expected_hash):
                        raise CDPackageError(f"artefato do pacote não corresponde ao catálogo: {identity}/{artifact}")
                    rel = item.get("path")
                    if (not isinstance(rel, str) or PurePosixPath(rel).is_absolute()
                            or ".." in PurePosixPath(rel).parts or not rel.startswith("cogs/tts/")):
                        raise CDPackageError(f"caminho de artefato inválido: {identity}")
                    _file(root, rel, item.get("sha256"), 20 * 1024 * 1024)
        if require_complete and (not score or not video or any(status[key] != "ready" for key in ("score", "thumbnail", "sample"))):
            raise CDPackageError(f"CD incompleto: {identity}")
        artifacts[identity] = status
    return {"ok": True, "songs": sorted(selected), "artifacts": artifacts}


def validate_artifact_references(root: Path | str, paths) -> None:
    """An uploaded binary must be referenced by its matching public manifest."""
    root = Path(root)
    _, videos = _map(root, VIDEOS, "songs")
    _, previews = _map(root, PREVIEWS)
    for rel in paths:
        path = PurePosixPath(rel)
        if str(path.parent) == "cogs/tts/cd/previews" and path.suffix == ".mp3":
            if path.stem not in previews:
                raise CDPackageError(f"MP3 enviado sem manifesto de amostra: {path.stem}")
            data = _file(root, rel, previews[path.stem].get("mp3_sha256"), 20 * 1024 * 1024)
            _audio(data, root / rel, path.stem, False)
        if str(path.parent) == "cogs/tts/cd/thumbnails" and path.suffix == ".jpg":
            matches = [video for video in videos.values() if video.get("video_id") == path.stem and video.get("thumbnail_sha256")]
            if not matches:
                raise CDPackageError(f"JPEG enviado sem metadados/hash: {path.stem}")
            for video in matches:
                _jpeg(_file(root, rel, video["thumbnail_sha256"], 2 * 1024 * 1024), path.stem)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def merge_cd_overlay(overlay_root: Path | str, base_root: Path | str, paths=None) -> list[str]:
    """Merge CD ids in an isolated overlay; preserve unrelated songs and ready audio.

    A changed score invalidates only its sample index; bytes remain available for
    rollback. A cover-only increment never alters sample metadata.
    """
    overlay, base = Path(overlay_root), Path(base_root)
    available = set(paths) if paths is not None else {p.relative_to(overlay).as_posix() for p in overlay.rglob("*") if p.is_file()}
    affected = []
    base_scores = _scores(base)
    incoming_scores = _scores(overlay) if SCORES in available else {}
    merged_scores = dict(base_scores)
    changed_scores = set()
    incoming_video_ids, incoming_preview_ids = set(), set()
    for identity, entry in incoming_scores.items():
        previous = base_scores.get(identity)
        if previous:
            old_revision, new_revision = previous.get("revision", 1), entry.get("revision", 1)
            if new_revision < old_revision:
                raise CDPackageError(f"revision anterior à instalada: {identity}")
            if score_sha256(previous) != score_sha256(entry):
                if new_revision <= old_revision:
                    raise CDPackageError(f"partitura alterada sem nova revision: {identity}")
                changed_scores.add(identity)
        merged_scores[identity] = entry
    if SCORES in available:
        _write(overlay / SCORES, list(merged_scores.values()))
        affected.append(SCORES)
    if VIDEOS in available:
        previous_raw, previous = _map(base, VIDEOS, "songs")
        incoming_raw, incoming = _map(overlay, VIDEOS, "songs")
        incoming_video_ids = set(incoming)
        songs = dict(previous)
        for identity, item in incoming.items():
            prior = previous.get(identity, {})
            combined = dict(prior)
            if prior.get("video_id") != item.get("video_id", prior.get("video_id")):
                combined.pop("thumbnail_sha256", None)
            combined.update(item)
            songs[identity] = combined
        _write(overlay / VIDEOS, {**previous_raw, **incoming_raw, "songs": songs})
        affected.append(VIDEOS)
    _, previous_previews = _map(base, PREVIEWS)
    refresh_previews = bool(set(incoming_scores) & set(previous_previews))
    merged_previews = dict(previous_previews)
    if PREVIEWS in available or changed_scores or refresh_previews:
        previous = previous_previews
        _, incoming = _map(overlay, PREVIEWS) if PREVIEWS in available else ({}, {})
        incoming_preview_ids = set(incoming)
        merged = {identity: item for identity, item in previous.items() if identity not in changed_scores}
        for identity in set(incoming_scores) & set(merged):
            retained = dict(merged[identity])
            if retained.get("manifest_version") == 2:
                retained["source_sha256"] = merged_scores[identity].get("source_sha256")
                if "revision" in retained:
                    retained["revision"] = merged_scores[identity].get("revision", 1)
            merged[identity] = retained
        merged.update(incoming)
        merged_previews = merged
        _write(overlay / PREVIEWS, merged)
        affected.append(PREVIEWS)
    # An old package manifest is a snapshot, so refresh inherited manifests when
    # a score/sample/cover increment changes that snapshot. Explicit incoming
    # manifests remain untouched and are checked against the resulting files.
    _, current_videos = _map(overlay if VIDEOS in available else base, VIDEOS, "songs")
    ids = set(incoming_scores)
    ids.update(incoming_video_ids)
    ids.update(incoming_preview_ids)
    for identity in ids:
        rel = f"cogs/tts/cd/packages/{identity}.json"
        if (overlay / rel).exists() or not (base / rel).is_file() or identity not in merged_scores:
            continue
        package = _json(base / rel, {})
        score = merged_scores[identity]
        video, preview = current_videos.get(identity, {}), merged_previews.get(identity, {})
        artifacts = {"score": {"status": "ready", "sha256": score_sha256(score)}}
        artifacts["sample"] = ({"status": "ready", "path": f"cogs/tts/cd/previews/{identity}.mp3", "sha256": preview["mp3_sha256"]}
                               if preview.get("score_sha256") == score_sha256(score) and preview.get("mp3_sha256")
                               else {"status": "pending"})
        artifacts["thumbnail"] = ({"status": "ready", "path": f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg", "sha256": video["thumbnail_sha256"]}
                                  if video.get("video_id") and video.get("thumbnail_sha256") else {"status": "pending"})
        package.update(manifest_version=1, id=identity, revision=score.get("revision", 1),
                       score_sha256=score_sha256(score), source_sha256=score.get("source_sha256"), artifacts=artifacts)
        _write(overlay / rel, package)
        affected.append(rel)
    return affected


def _zip_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    infos = archive.infolist()
    if len(infos) > 500:
        raise CDPackageError("ZIP excede 500 entradas")
    seen, total, result = {}, 0, []
    for info in infos:
        name = info.filename.replace("\\", "/")
        rel = PurePosixPath(name)
        parts = rel.parts
        if (rel.is_absolute() or not parts or ".." in parts or parts[0].endswith(":")
                or any(part != part.strip() for part in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise CDPackageError(f"caminho ZIP inválido: {name!r}")
        key = unicodedata.normalize("NFC", rel.as_posix()).casefold()
        kind = (info.external_attr >> 16) & 0o170000
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
            raise CDPackageError(f"entrada ZIP não permitida: {name}")
        directory = info.is_dir() or kind == stat.S_IFDIR
        if key in seen or any(seen.get("/".join(key.split("/")[:i])) is False for i in range(1, len(parts))):
            raise CDPackageError(f"caminho ZIP duplicado/conflitante: {name}")
        if not directory and any(existing.startswith(key + "/") for existing in seen):
            raise CDPackageError(f"arquivo/diretório conflitante: {name}")
        seen[key] = directory
        if directory:
            continue
        lowered = tuple(part.casefold() for part in parts)
        basename = lowered[-1]
        if (lowered[0] in {".git", "data", "logs", "node_modules", "secrets", "__pycache__"}
                or lowered[:2] == (".github", "workflows")
                or ((basename == ".env" or basename.startswith(".env.")) and basename not in {".env.example", ".env.sample", ".env.template"})
                or "google-credentials" in basename or "youtube-cookies" in basename):
            raise CDPackageError(f"caminho protegido: {name}")
        total += info.file_size
        if info.file_size > 20 * 1024 * 1024 or total > 100 * 1024 * 1024 or info.file_size / max(1, info.compress_size) > 200:
            raise CDPackageError("ZIP excede os limites do updater")
        result.append((info, rel.as_posix()))
    return result


def validate_package(zip_path: Path | str, base_dir: Path | str | None = None, *,
                     require_complete: bool = False, audio_probe: bool = False,
                     require_cd_only: bool = False, ids=None) -> dict:
    zip_path = Path(zip_path)
    if not zip_path.is_file() or zip_path.stat().st_size > 25 * 1024 * 1024:
        raise CDPackageError("ZIP ausente ou excede 25 MiB")
    with tempfile.TemporaryDirectory(prefix="osaka-cd-validate-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(zip_path) as archive:
            members = _zip_members(archive)
            paths = [rel for _, rel in members]
            def cd_artifact(rel):
                if rel in (SCORES, VIDEOS, PREVIEWS):
                    return True
                path = PurePosixPath(rel)
                return ((str(path.parent) == "cogs/tts/cd/thumbnails" and path.suffix == ".jpg" and _VIDEO.fullmatch(path.stem))
                        or (str(path.parent) == "cogs/tts/cd/previews" and path.suffix == ".mp3" and _ID.fullmatch(path.stem))
                        or (str(path.parent) == "cogs/tts/cd/packages" and path.suffix == ".json" and _ID.fullmatch(path.stem)))
            if require_cd_only and any(not cd_artifact(rel) for rel in paths):
                raise CDPackageError("rota CD aceita somente artefatos de catálogo CD")
            for info, rel in members:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(info))
        incoming_ids = set(_scores(root)) | set(_map(root, VIDEOS, "songs")[1]) | set(_map(root, PREVIEWS)[1])
        incoming_ids.update(PurePosixPath(rel).stem for rel in paths if str(PurePosixPath(rel).parent) == "cogs/tts/cd/packages")
        if base_dir is not None:
            merge_cd_overlay(root, base_dir, paths)
            # Fill only missing CD files from base; never modify base_dir.
            base = Path(base_dir)
            for rel in (SCORES, VIDEOS, PREVIEWS):
                if not (root / rel).exists() and (base / rel).is_file():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(base / rel, root / rel)
            if (base / "cogs/tts/cd").is_dir():
                for source in (base / "cogs/tts/cd").rglob("*"):
                    rel = source.relative_to(base)
                    if source.is_file() and not source.is_symlink() and not (root / rel).exists():
                        (root / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, root / rel)
            for rel in paths:
                if rel.startswith("cogs/tts/cd/previews/") and rel.endswith(".mp3"):
                    incoming_ids.add(PurePosixPath(rel).stem)
                if rel.startswith("cogs/tts/cd/thumbnails/") and rel.endswith(".jpg"):
                    _, videos = _map(root, VIDEOS, "songs")
                    incoming_ids.update(identity for identity, video in videos.items() if video.get("video_id") == PurePosixPath(rel).stem)
        validate_artifact_references(root, paths)
        report = validate_cd_tree(root, incoming_ids if ids is None else ids,
                                  require_complete=require_complete, audio_probe=audio_probe)
        return {**report, "files": paths, "zip_sha256": _digest(zip_path.read_bytes()), "zip_path": str(zip_path.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--audio-probe", action="store_true")
    parser.add_argument("--song", action="append", dest="ids", help="verificar completude de ids específicos")
    options = parser.parse_args()
    try:
        report = (validate_cd_tree(options.path, options.ids, require_complete=options.require_complete, audio_probe=options.audio_probe)
                  if options.tree else validate_package(options.path, options.base, ids=options.ids, require_complete=options.require_complete, audio_probe=options.audio_probe))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except (CDPackageError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
