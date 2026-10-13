"""CD contract adapter; all validation lives in the same file shipped to Termux."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def contract():
    path = Path(__file__).resolve().parents[2] / "deploy/ttv-cd/package_validation.py"
    spec = importlib.util.spec_from_file_location("osaka_cd_package_validation", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("validador CD indisponível")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_cd_path(path: str) -> bool:
    return path in {"cogs/tts/cd_song_scores.json", "cogs/tts/cd_song_videos.json"} or path.startswith("cogs/tts/cd/")


def _changed_ids(module, overlay: Path, base: Path, paths: set[str]) -> set[str]:
    ids = set()
    for rel, key in ((module.SCORES, "id"), (module.VIDEOS, "songs"), (module.PREVIEWS, None)):
        if rel not in paths:
            continue
        incoming = module._json(overlay / rel, [] if key == "id" else {})
        previous = module._json(base / rel, [] if key == "id" else {})
        if key == "id":
            incoming = {item["id"]: item for item in incoming}
            previous = {item["id"]: item for item in previous}
        elif key:
            incoming, previous = incoming.get(key, {}), previous.get(key, {})
        ids.update(identity for identity, value in incoming.items() if value != previous.get(identity))
    for rel in paths:
        if rel.startswith("cogs/tts/cd/packages/") and rel.endswith(".json"):
            ids.add(Path(rel).stem)
        if rel.startswith("cogs/tts/cd/previews/") and rel.endswith(".mp3"):
            ids.add(Path(rel).stem)
        if rel.startswith("cogs/tts/cd/thumbnails/") and rel.endswith(".jpg"):
            video_id = Path(rel).stem
            for root in (overlay, base):
                _, videos = module._map(root, module.VIDEOS, "songs")
                ids.update(identity for identity, item in videos.items() if item.get("video_id") == video_id)
    return ids


def prepare_cd_overlay(extracted_files: list[tuple[Path, Path]], base: Path) -> list[tuple[Path, Path]]:
    paths = {rel.as_posix() for _, rel in extracted_files}
    cd_files = [(source, rel) for source, rel in extracted_files if is_cd_path(rel.as_posix())]
    if not cd_files:
        return extracted_files
    module = contract()
    # Extraction supplies every source under the same repository-relative root.
    source, rel = cd_files[0]
    overlay = source
    for _ in rel.parts:
        overlay = overlay.parent
    ids = _changed_ids(module, overlay, base, paths)
    merged_paths = module.merge_cd_overlay(overlay, base, paths)
    result = {rel.as_posix(): (source, rel) for source, rel in extracted_files}
    for rel in merged_paths:
        result[rel] = (overlay / rel, Path(rel))
    # Validate exactly the candidate tree, with retained assets and merged indices.
    # No writes reach the live bot tree or the comparison clone.
    with tempfile.TemporaryDirectory(prefix="updater-cd-check-") as temp:
        staged = Path(temp)
        for current in (base, overlay):
            for rel in (module.SCORES, module.VIDEOS, module.PREVIEWS):
                path = current / rel
                if path.is_file() and not path.is_symlink():
                    target = staged / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            cd_dir = current / "cogs/tts/cd"
            if cd_dir.is_dir():
                for path in cd_dir.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        target = staged / path.relative_to(current)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
        module.validate_artifact_references(staged, paths)
        module.validate_cd_tree(staged, ids, audio_probe=True)
    return list(result.values())


def validate_candidate_tree(root: Path, changed: list[str]) -> dict:
    if not any(is_cd_path(path) for path in changed):
        return {"ok": True, "songs": []}
    module = contract()
    # Inspect only changed song ids: historical incomplete assets unrelated to
    # this increment must not block a valid new CD.
    with tempfile.TemporaryDirectory(prefix="updater-cd-base-") as temp:
        base = Path(temp)
        for rel in (module.SCORES, module.VIDEOS, module.PREVIEWS):
            result = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{rel}"], capture_output=True)
            if not result.returncode:
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(result.stdout)
        ids = _changed_ids(module, root, base, set(changed))
        return module.validate_cd_tree(root, ids, audio_probe=True)


if __name__ == "__main__":
    try:
        result = validate_candidate_tree(Path(sys.argv[1]), sys.argv[2:])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"validação CD recusada: {exc}", file=sys.stderr)
        raise SystemExit(1)
