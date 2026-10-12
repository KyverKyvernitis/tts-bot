"""Consume native Discord multipart payloads, including changing cached covers."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.http import handle_message_parameters

import config
import pytest
from cogs.tts import cd_covers
from cogs.tts.interface.painel_canto import CDPanelView
from cogs.tts.melodies import DEFAULT_MELODY_ID, list_melodies
from test_cd_panel import walk as _walk_components
from test_cd_panel import cog_for, interaction


@pytest.fixture
def local_cover_cache(tmp_path, monkeypatch):
    """A real JPEG fixture in a temporary cache, never a product placeholder."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("requires FFmpeg to validate image upload serialization")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                             "-i", "color=black:s=16x16", "-frames:v", "1", "-threads", "1",
                             "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                            capture_output=True, timeout=10, check=True)
    data = result.stdout
    folder = tmp_path / "cd" / "thumbnails"
    folder.mkdir(parents=True)
    songs = {}
    for number, song in enumerate(list_melodies()[1:], start=1):
        identity = f"test{number:07d}"
        (folder / (identity + ".jpg")).write_bytes(data)
        songs[song.id] = {"video_id": identity, "thumbnail_sha256": hashlib.sha256(data).hexdigest()}
    (tmp_path / "cd_song_videos.json").write_text(json.dumps({"songs": songs}))
    monkeypatch.setattr(cd_covers, "_DATA", tmp_path)
    monkeypatch.delenv("TTV_CD_THUMBNAIL_CACHE_DIR", raising=False)


def _consume_sdk(parameters):
    with handle_message_parameters(**parameters) as request:
        payload = request.payload
        if payload is None:
            payload = json.loads(next(part["value"] for part in request.multipart
                                      if part["name"] == "payload_json"))
        raw = [file.fp.read() for file in request.files or ()]
        assert all(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9") for data in raw)
        assert discord.MessageFlags._from_value(payload["flags"]).components_v2
        thumbs = [part for part in _walk_components(payload["components"]) if part["type"] == 11]
        for thumb in thumbs:
            url = thumb["media"]["url"]
            if url.startswith("attachment://"):
                assert any(url == "attachment://" + item["filename"] for item in payload["attachments"])
        return payload, raw


def test_panel_sends_saved_song_with_offline_cached_attachment(local_cover_cache):
    async def scenario():
        song = list_melodies()[1]
        cog = cog_for({"ttv_melody_id": song.id}, online=False)
        view = CDPanelView(cog, 10, 20)
        await view.refresh()
        assert view.selected_melody_id == song.id
        files = view.panel_files()
        try:
            payload, raw = _consume_sdk({"view": view, "files": files,
                "allowed_mentions": discord.AllowedMentions.none()})
            assert len(raw) == 1 and payload["attachments"][0]["filename"] == files[0].filename
            assert view.preview_button.disabled and not view.melody_select.disabled
        finally:
            for file in files:
                file.close()
            view.stop()
    asyncio.run(scenario())


def test_selecting_songs_replaces_cover_and_returning_to_default_clears_attachments(local_cover_cache):
    async def scenario():
        cog = cog_for(online=False)
        view = CDPanelView(cog, 10, 20)
        await view.refresh()
        assert view.selected_melody_id == DEFAULT_MELODY_ID
        names = []
        for song in list_melodies()[1:]:
            inter = interaction()

            async def consume(**kwargs):
                # edit_message accepts attachments; SDK send serialization uses
                # files for freshly uploaded attachments, preserving their ids.
                files = kwargs.pop("attachments")
                payload, raw = _consume_sdk({**kwargs, "files": files})
                assert len(raw) == 1 and len(payload["attachments"]) == 1
                names.append(payload["attachments"][0]["filename"])

            inter.response.edit_message = AsyncMock(side_effect=consume)
            view.melody_select._values = [song.id]
            await view.select_melody(inter)
            assert view.selected_melody_id == song.id and not view.apply_button.disabled
        assert len(set(names)) == 10
        default = interaction()
        view.melody_select._values = [DEFAULT_MELODY_ID]
        await view.select_melody(default)
        assert default.response.edit_message.await_args.kwargs["attachments"] == []
        assert view.apply_button.disabled
        cog._cd_preview_mp3.assert_not_awaited()
        cog._set_user_tts_and_refresh.assert_not_awaited()
        view.stop()

    asyncio.run(scenario())
