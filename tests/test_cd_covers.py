from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from PIL import Image

from cogs.tts import cd_covers


def _jpeg() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (48, 32), "blue").save(stream, format="JPEG")
    return stream.getvalue()


@pytest.fixture
def cached_cover(tmp_path, monkeypatch):
    data = tmp_path / "data"
    covers = data / "cd/thumbnails"
    covers.mkdir(parents=True)
    raw = _jpeg()
    (covers / "19y8YTbvri8.jpg").write_bytes(raw)
    (data / "cd_song_videos.json").write_text(json.dumps({"songs": {"mesmerizer": {
        "video_id": "19y8YTbvri8", "thumbnail_sha256": hashlib.sha256(raw).hexdigest()}}}))
    monkeypatch.setattr(cd_covers, "_DATA", data)
    monkeypatch.delenv("TTV_CD_THUMBNAIL_CACHE_DIR", raising=False)
    return covers, raw


def test_local_file_is_fresh_and_has_safe_attachment_name(cached_cover):
    left = cd_covers.cached_cover_file("mesmerizer")
    right = cd_covers.cached_cover_file("mesmerizer")
    try:
        assert left is not right
        assert left.fp is not right.fp
        assert left.filename == "ttv-cd-mesmerizer.jpg"
        assert left.fp.read() == cached_cover[1]
        assert cd_covers.cover_attachment_url("mesmerizer") == "attachment://ttv-cd-mesmerizer.jpg"
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("identifier", ["../mesmerizer", "mesmerizer.jpg", "", "https://example.com", None])
def test_invalid_ids_never_open_a_file(cached_cover, identifier):
    assert cd_covers.cached_cover_file(identifier) is None
    assert cd_covers.cover_attachment_url(identifier) == ""


@pytest.mark.parametrize("kind", ["truncated", "wrong_hash", "oversize", "symlink"])
def test_invalid_cache_is_not_sent(cached_cover, kind, tmp_path):
    folder, raw = cached_cover
    path = folder / "19y8YTbvri8.jpg"
    if kind == "truncated":
        path.write_bytes(raw[:-2])
    elif kind == "wrong_hash":
        path.write_bytes(raw[:-2] + b"wrong\xff\xd9")
    elif kind == "oversize":
        path.write_bytes(b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024) + b"\xff\xd9")
    else:
        path.unlink()
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(raw)
        path.symlink_to(outside)
    assert cd_covers.cached_cover_file("mesmerizer") is None
    assert cd_covers.cover_attachment_url("mesmerizer") == ""


def test_pinned_thumbnail_url_does_not_call_network(cached_cover, monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        raise AssertionError("network must never run during panel rendering")
    monkeypatch.setattr(socket, "create_connection", blocked)
    assert cd_covers.remote_thumbnail_url("mesmerizer") == "https://i.ytimg.com/vi/19y8YTbvri8/hqdefault.jpg"
    assert cd_covers.remote_thumbnail_url("unknown") == ""


def _helper():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("cd_thumbnail_helper_test", root / "deploy/ttv-cd/cache-thumbnails.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_most_viewed_choice_rejects_wrong_song_and_versions():
    helper = _helper()
    item = {"title": "Tell Your World", "title_aliases": ["tell your world"]}
    candidates = [
        {"video_id": "PqJNc9KVIZE", "title": "livetune feat. Hatsune Miku — Tell Your World", "view_count": 500},
        {"video_id": "vKs0wc0h_HY", "title": "Tell Your World live concert", "view_count": 900},
        {"video_id": "sH16drUAcco", "title": "Tell Your World English Cover", "view_count": 1000},
        {"video_id": "19y8YTbvri8", "title": "Mesmerizer", "view_count": 9000},
        {"video_id": "rlfrgoPwrFo", "title": "Tell Your World", "view_count": 100},
    ]
    assert helper.choose_most_viewed(item, candidates)["video_id"] == "PqJNc9KVIZE"
    assert helper.choose_most_viewed(item, [dict(candidates[0], view_count=None)]) is None


def test_melt_does_not_match_another_song():
    helper = _helper()
    item = {"title_aliases": ["melt", "メルト"], "excluded_title_terms": ["ice cream", "CPK"]}
    assert not helper._matches(item, {"title": "Melt ice cream / Hatsune Miku"})
    assert helper._matches(item, {"title": "Melt | Hatsune Miku Original"})


def test_bundled_archive_sources_are_exact_music_sources():
    root = Path(__file__).resolve().parents[1]
    helper = _helper()
    for name in helper.ARCHIVE_FILES:
        assert (root / "deploy/ttv-cd/archive" / name).read_bytes() == (root / "cogs/musica/runtime_telefone/agente" / name).read_bytes()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="real image converter required")
def test_standalone_termux_kit_validates_existing_covers_and_exports_bot_patch(tmp_path):
    """Uses real FFmpeg + real ArchiveMixin with only the installable kit."""
    root = Path(__file__).resolve().parents[1]
    kit = tmp_path / "kit"
    shutil.copytree(root / "deploy/ttv-cd", kit / "deploy/ttv-cd", ignore=shutil.ignore_patterns("__pycache__"))
    cache = tmp_path / "phone-cache"
    cache.mkdir()
    raw = _jpeg()
    catalog = json.loads((kit / "deploy/ttv-cd/cd_song_videos.json").read_text())
    for item in catalog["songs"].values():
        (cache / f"{item['video_id']}.jpg").write_bytes(raw)
    output = tmp_path / "covers.zip"
    env = dict(os.environ, HTTPS_PROXY="http://127.0.0.1:1", HTTP_PROXY="http://127.0.0.1:1")
    done = subprocess.run([sys.executable, str(kit / "deploy/ttv-cd/cache-thumbnails.py"), "--cache-dir", str(cache),
                           "--export-zip", str(output)], cwd=tmp_path, env=env, capture_output=True, timeout=45, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert report["ok"] and len(report["prepared"]) == 10 and not report["failures"]
    assert report["panel_network_requests"] == 0
    assert not report["worker_runtime_changed"]
    with zipfile.ZipFile(output) as zipped:
        assert len(zipped.namelist()) == 11
        assert zipped.testzip() is None
        published = json.loads(zipped.read("cogs/tts/cd_song_videos.json"))
        for item in published["songs"].values():
            data = zipped.read(f"cogs/tts/cd/thumbnails/{item['video_id']}.jpg")
            assert data == raw  # validation does not re-encode a cached JPEG each run
            assert hashlib.sha256(data).hexdigest() == item["thumbnail_sha256"]
