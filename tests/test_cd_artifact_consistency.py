"""Real audio/Discord serialization for independently versioned CD artifacts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from cogs.tts import cd_covers, cd_previews, melodies
from cogs.tts.cd_colors import cover_accent
from cogs.tts.interface import painel_canto as panel
from cogs.tts.interface.application_emojis import ApplicationSelectEmojis
from test_cd_panel import cog_for, interaction


PREFERENCES = {"ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR",
               "ttv_pitch_semitones": 0, "ttv_speech_rate": 1}


@pytest.fixture
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(melodies, "_SCORE_FILE", tmp_path / "cd_song_scores.json")
    monkeypatch.setattr(melodies, "_CATALOG_SIGNATURE", (-1, -1))
    monkeypatch.setattr(melodies, "_MELODIES", (melodies._DEFAULT_MELODY,))
    monkeypatch.setattr(cd_covers, "_DATA", tmp_path)
    monkeypatch.setattr(cd_previews, "_ROOT", tmp_path / "cd" / "previews")
    monkeypatch.delenv("TTV_CD_THUMBNAIL_CACHE_DIR", raising=False)
    return tmp_path


def entry(identity="new-cd"):
    return {"id": identity, "name": "New CD", "artist": "Composer", "excerpt": "Test excerpt",
            "source_url": "https://example.org/song.mid", "source_sha256": "a" * 64,
            "revision": 1, "duration_ms": 1400,
            "notes": [{"midi_note": 60, "start_ms": 100, "duration_ms": 500, "velocity": 100},
                      {"midi_note": 64, "start_ms": 800, "duration_ms": 500, "velocity": 100}]}


def install_scores(root, entries):
    (root / "cd_song_scores.json").write_text(json.dumps(entries), encoding="utf-8")
    return melodies.list_melodies()


@pytest.fixture
def real_mp3():
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required for a real MP3 preview")
    result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                             "sine=frequency=440:duration=1.4", "-c:a", "libmp3lame",
                             "-f", "mp3", "pipe:1"], capture_output=True, check=True, timeout=15)
    return result.stdout


def install_preview(root, song, audio):
    target = root / "cd" / "previews"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{song['id']}.mp3").write_bytes(audio)
    index = {song["id"]: {"manifest_version": 2, "id": song["id"],
                         "score_sha256": melodies.score_sha256(melodies.catalog_score(song)),
                         "source_sha256": song["source_sha256"],
                         "mp3_sha256": hashlib.sha256(audio).hexdigest(),
                         "voice_id": "kasane-teto", "language": "pt-BR",
                         "pitch_semitones": 0, "speech_rate": 1}}
    (target / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return index


def install_cover(root, identity, color):
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(stream, "JPEG")
    data = stream.getvalue()
    folder = root / "cd" / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ABCDEFGHIJK.jpg").write_bytes(data)
    (root / "cd_song_videos.json").write_text(json.dumps({"version": 1, "songs": {
        identity: {"video_id": "ABCDEFGHIJK", "thumbnail_sha256": hashlib.sha256(data).hexdigest()}}}))
    return data


def test_actual_score_change_invalidates_sample_but_cover_change_preserves_it(isolated_catalog, real_mp3):
    root = isolated_catalog
    song = entry()
    install_scores(root, [song])
    install_preview(root, song, real_mp3)
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) == real_mp3
    red = install_cover(root, song["id"], "red")
    cover_hash = cd_covers.cover_revision(song["id"])
    blue = install_cover(root, song["id"], "blue")
    assert cd_covers.cover_revision(song["id"]) != cover_hash
    assert cover_accent(red) != cover_accent(blue)
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) == real_mp3
    song["notes"][0]["midi_note"] = 62  # Same original MIDI provenance, different singing payload.
    install_scores(root, [song])
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) is None


def test_legacy_source_hash_cannot_bind_an_unknown_score(isolated_catalog, real_mp3):
    song = entry()
    install_scores(isolated_catalog, [song])
    index = install_preview(isolated_catalog, song, real_mp3)
    metadata = index[song["id"]]
    digest = metadata["score_sha256"]
    metadata.pop("manifest_version")
    metadata["score_sha256"] = song["source_sha256"]
    target = isolated_catalog / "cd" / "previews" / "index.json"
    target.write_text(json.dumps(index))
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) is None
    # Explicit migration requires the canonical content identity as well.
    metadata["score_content_sha256"] = digest
    target.write_text(json.dumps(index))
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) == real_mp3


@pytest.mark.parametrize("field,value", [("pitch_semitones", 1), ("speech_rate", 1.25),
                                       ("language", "en-US"), ("id", "other-cd"),
                                       ("mp3_sha256", "b" * 64)])
def test_declared_synth_profile_and_audio_digest_are_checked(isolated_catalog, real_mp3, field, value):
    song = entry()
    install_scores(isolated_catalog, [song])
    index = install_preview(isolated_catalog, song, real_mp3)
    index[song["id"]][field] = value
    (isolated_catalog / "cd" / "previews" / "index.json").write_text(json.dumps(index))
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) is None


def test_hash_matched_non_audio_and_unsafe_ids_are_not_playable(isolated_catalog):
    song = entry()
    install_scores(isolated_catalog, [song])
    install_preview(isolated_catalog, song, b"ID3" + b"x" * 1024)
    assert cd_previews.prepared_sample(song["id"], PREFERENCES) is None
    assert cd_previews.prepared_sample("../new-cd", PREFERENCES) is None
    assert cd_previews.prepared_sample(song["id"], {**PREFERENCES, "ttv_voice_id": "hatsune-miku"}) is None


@pytest.mark.parametrize("mutation", ["overlap", "outside_duration", "bad_hash", "boolean_pitch"])
def test_invalid_reload_preserves_entire_previous_catalog(isolated_catalog, mutation):
    good = entry()
    install_scores(isolated_catalog, [good])
    prior = melodies.get_melody(good["id"])
    bad = deepcopy(good)
    if mutation == "overlap":
        bad["notes"][1]["start_ms"] = 200
    elif mutation == "outside_duration":
        bad["duration_ms"] = 1000
    elif mutation == "bad_hash":
        bad["score_sha256"] = "0" * 64
    else:
        bad["notes"][0]["midi_note"] = True
    install_scores(isolated_catalog, [bad])
    assert melodies.get_melody(good["id"]) == prior


def test_artifact_arrival_changes_watcher_signature_and_video_recovery(isolated_catalog, real_mp3):
    song = entry()
    install_scores(isolated_catalog, [song])
    before = melodies.catalog_signature()
    install_preview(isolated_catalog, song, real_mp3)
    after_sample = melodies.catalog_signature()
    assert after_sample != before
    data = install_cover(isolated_catalog, song["id"], "red")
    assert melodies.catalog_signature() != after_sample
    assert cd_covers._cover_bytes(song["id"]) == data
    (isolated_catalog / "cd_song_videos.json").write_text("{")
    assert cd_covers._cover_bytes(song["id"]) == data  # Keep validated metadata during interrupted write.


def test_large_catalog_paginates_and_expired_refresh_never_reactivates(isolated_catalog):
    install_scores(isolated_catalog, [entry(f"song-{number}") for number in range(61)])

    async def scenario():
        cog = cog_for({"ttv_melody_id": "song-50"}, online=False)
        view = panel.CDPanelView(cog, 10, 20, timeout=30)
        await view.refresh()
        assert view._page == 2 and len(view.melody_select.options) == 12
        assert any(option.value == "song-50" and option.default for option in view.melody_select.options)
        await view.previous_page(interaction())
        assert view._page == 1 and len(view.melody_select.options) == 25
        await view.next_page(interaction())
        assert view._page == 2
        # User activity respects the requested lifetime; catalog updates do not extend it.
        assert view.expires_at_monotonic - time.monotonic() <= 30
        view.expires_at_monotonic = 0
        await view.refresh()
        assert view.melody_select.disabled and view.preview_button.disabled and view.apply_button.disabled
        assert view.previous_button.disabled and view.next_button.disabled
        assert view.expires_at_monotonic == 0
        view.stop()
    asyncio.run(scenario())


def test_real_prepared_sample_button_works_with_phone_offline(isolated_catalog, real_mp3):
    song = entry()
    install_scores(isolated_catalog, [song])
    install_preview(isolated_catalog, song, real_mp3)

    async def scenario():
        cog = cog_for({**PREFERENCES, "ttv_melody_id": song["id"]}, online=False)
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        assert not view.preview_button.disabled
        event = interaction()
        await view.preview_melody(event)
        cog._cd_preview_mp3.assert_not_awaited()
        audio = event.followup.send.await_args.kwargs["file"].fp.read()
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                                 input=audio, capture_output=True, timeout=15)
        assert decoded.returncode == 0 and len(decoded.stdout) > 44100
        assert "Kasane Teto" in event.followup.send.await_args.kwargs["content"]
        view.stop()
    asyncio.run(scenario())


def test_rhythm_includes_leading_internal_and_trailing_silence(isolated_catalog):
    song = entry()
    install_scores(isolated_catalog, [song])
    assert melodies.format_score_rhythm(melodies.get_melody(song["id"])) == "𝄽0,1s · 0,5s · 𝄽0,2s · 0,5s · 𝄽0,1s"


def test_changed_cover_creates_one_new_reusable_emoji_without_refreshing_expired_panels(isolated_catalog):
    song = entry()
    install_scores(isolated_catalog, [song])
    install_cover(isolated_catalog, song["id"], "red")

    async def scenario():
        remote = []

        async def create(*, name, image):
            with Image.open(io.BytesIO(image)) as picture:
                assert picture.size == (128, 128)
            emoji = SimpleNamespace(name=name, id=100000000000000000 + len(remote))
            remote.append(emoji)
            return emoji

        bot = SimpleNamespace(fetch_application_emojis=AsyncMock(side_effect=lambda: list(remote)),
                              create_application_emoji=AsyncMock(side_effect=create))
        view = panel.CDPanelView(cog_for(), 10, 20)
        view.message = SimpleNamespace(edit=AsyncMock())
        view.expires_at_monotonic = 0
        manager = ApplicationSelectEmojis("osakacd", cd_covers._cover_bytes, lambda: [song["id"]],
                                          version_for_id=cd_covers.cover_revision)
        manager.schedule(bot, view)
        await manager._task
        old = manager.emoji(song["id"])
        install_cover(isolated_catalog, song["id"], "blue")
        assert manager.emoji(song["id"]) is None
        manager.schedule(bot, view)
        await manager._task
        assert manager.emoji(song["id"]).id != old.id and len(remote) == 2
        manager.schedule(bot, view)
        assert len(remote) == 2
        view.message.edit.assert_not_awaited()
        view.stop()
    asyncio.run(scenario())
