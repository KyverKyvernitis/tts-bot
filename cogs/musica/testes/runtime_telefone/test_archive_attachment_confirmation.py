"""Discord filename normalization must not strand otherwise valid archives."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente.archive_manifest import (
    MANIFEST_FILENAME, inline_manifest, validate_manifest,
)
from cogs.musica.runtime_telefone.agente.archive_pipeline import ArchivePipelineMixin
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, _archive_metadata
from .test_arquivo_segmentado import FakeAttachment, FakeForum, KEY, UploadWorker


async def _no_delay(*_args, **_kwargs):
    pass


def _confirmation_worker(raw):
    async def get_message(channel_id, message_id):
        assert (channel_id, message_id) == (40, 30)
        return raw
    worker = ArchivePipelineMixin()
    worker.client = SimpleNamespace(http=SimpleNamespace(get_message=get_message))
    return worker


def _raw_attachment(filename="Canção de teste-abcdef123456.ogg", size=100):
    return {"author": {"id": "5"}, "attachments": [
        {"id": "9", "filename": filename, "size": size,
         "url": "https://cdn.discordapp.com/attachments/40/9/audio.ogg"},
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_name", [
    "Canção de teste-abcdef123456.ogg",
    "Canção_de_teste-abcdef123456.ogg",
])
async def test_attachment_confirmation_accepts_exact_and_discord_space_replacement(wire_name):
    raw = _raw_attachment(wire_name)
    result = await _confirmation_worker(raw)._archive_confirm_attachment(
        40, 30, filename="Canção de teste-abcdef123456.ogg", size_bytes=100, bot_id=5,
    )
    assert result == raw["attachments"][0]
    assert result["filename"] == wire_name


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["size", "name", "case", "extra_separator", "author", "cdn", "ambiguous"])
async def test_attachment_confirmation_keeps_size_identity_and_ownership_checks(change, monkeypatch):
    raw = _raw_attachment("Canção_de_teste-abcdef123456.ogg")
    attachment = raw["attachments"][0]
    if change == "size":
        attachment["size"] = 99
    elif change == "name":
        attachment["filename"] = "outro-arquivo-abcdef123456.ogg"
    elif change == "case":
        attachment["filename"] = attachment["filename"].lower()
    elif change == "extra_separator":
        attachment["filename"] = "Canção__de_teste-abcdef123456.ogg"
    elif change == "author":
        raw["author"]["id"] = "6"
    elif change == "cdn":
        attachment["url"] = "https://example.com/attachments/40/9/audio.ogg"
    else:
        raw["attachments"].append({**attachment, "id": "10"})
    module = ArchivePipelineMixin._archive_confirm_attachment.__globals__
    monkeypatch.setattr(module["asyncio"], "sleep", _no_delay)
    with pytest.raises(ValueError):
        await _confirmation_worker(raw)._archive_confirm_attachment(
            40, 30, filename="Canção de teste-abcdef123456.ogg", size_bytes=100, bot_id=5,
        )


class NormalizingUploadWorker(UploadWorker):
    def __init__(self, *, segment_count=2):
        super().__init__()
        self.segment_count = segment_count
        self.audio_uploads = 0
        self.manifest_uploads = 0

    def attachment(self, file):
        self.next_attachment_id += 1
        if file.filename == MANIFEST_FILENAME:
            self.manifest_uploads += 1
        else:
            self.audio_uploads += 1
        file.fp.seek(0)
        return FakeAttachment(self.next_attachment_id, file.filename.replace(" ", "_"), file.fp.read())

    async def _archive_prepare_segments(self, audio, folder, **kwargs):
        parts = await super()._archive_prepare_segments(audio, folder, **kwargs)
        if self.segment_count == 1:
            parts = parts[:1]
            parts[0]["duration"] = 800.0
        return parts


def _item(title):
    return {"key": KEY, "guild_id": 1, "channel_id": 20, "emoji": "🎵", "retry": False,
            "track": {"title": title, "source": "YouTube", "duration": 800,
                      "webpage_url": "https://youtube.com/watch?v=long"}}


def _prepare_upload(monkeypatch, tmp_path, *, segment_count=2):
    from cogs.musica.runtime_telefone.agente import archive_staging
    monkeypatch.setattr(archive_staging.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ArchiveMixin._archive_one.__globals__["discord"], "ForumChannel", FakeForum)
    return NormalizingUploadWorker(segment_count=segment_count)


async def _assert_confirmed_archive(worker, result, title):
    root = worker.forum.threads[0].messages[0]
    raw = await worker.raw(40, root.id)
    manifest = validate_manifest(inline_manifest(raw), archive_key=KEY, reference=result["reference"])
    assert root.embeds[0].title == title
    assert _archive_metadata(raw, KEY, 5, result["reference"])["archive_segments"] == result["reference"]["segments"]
    manifest_attachment = next(a for a in root.attachments if a.filename == MANIFEST_FILENAME)
    assert json.loads(manifest_attachment.data) == manifest
    assert manifest["audio_codec"] == "opus" and manifest["audio_sample_rate"] == 48000
    assert manifest["audio_channels"] == 2 and manifest["audio_abr"] == 192
    for part in manifest["segments"]:
        message = await worker.forum.threads[0].fetch_message(part["reference"]["message_id"])
        attachment = next(a for a in message.attachments if a.id == part["reference"]["attachment_id"])
        assert part["filename"] == attachment.filename
        assert " " not in part["filename"]
        assert part["size_bytes"] == attachment.size
        assert part["sha256"] == hashlib.sha256(attachment.data).hexdigest()
        assert attachment.data == bytes([part["order"]]) * 100


@pytest.mark.asyncio
@pytest.mark.parametrize("title,segment_count", [
    ("Heaven Pierce Her - In the Presence of a King", 1),
    ("Kate Bush - Army Dreamers", 1),
    ("Canção com acentuação e underscore_preservado", 2),
])
async def test_normalized_upload_confirms_real_names_without_changing_audio_or_title(title, segment_count, tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path, segment_count=segment_count)
    result = await worker._archive_one(_item(title))
    assert result["status"] == "done" and result["presentation"] == 8
    assert worker.created == 1 and worker.audio_uploads == segment_count
    assert worker.manifest_uploads == 1 and worker.downloads == 1
    assert not worker.folders[-1].exists()
    await _assert_confirmed_archive(worker, result, title)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["first_audio_ack", "manifest_upload", "final_manifest_ack"])
async def test_normalized_incomplete_post_retry_reuses_audio_and_thread(interruption, tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path)
    title = "Canção longa com espaços"
    item = _item(title)
    confirm = worker._archive_confirm_attachment
    if interruption == "manifest_upload":
        worker.fail_manifest = True
    else:
        async def lose_ack(channel_id, message_id, **kwargs):
            result = await confirm(channel_id, message_id, **kwargs)
            if ((interruption == "first_audio_ack" and message_id == 30 and kwargs["filename"] != MANIFEST_FILENAME)
                    or (interruption == "final_manifest_ack" and kwargs["filename"] == MANIFEST_FILENAME)):
                raise RuntimeError("ACK interrompido")
            return result
        worker._archive_confirm_attachment = lose_ack
    with pytest.raises(RuntimeError, match="interrompido"):
        await worker._archive_one(item)
    assert worker.created == 1 and worker.folders[-1].exists()
    starter_audio = worker.forum.threads[0].messages[0].attachments[0]
    assert "_" in starter_audio.filename
    worker._archive_confirm_attachment = confirm
    result = await worker._archive_one({**item, "retry": True})
    assert result["status"] == "done"
    assert worker.created == 1 and worker.sent_segments == 1
    assert worker.audio_uploads == 2 and worker.downloads == 1
    assert len(worker.forum.threads[0].messages) == 2
    assert worker.forum.threads[0].messages[0].attachments[0].id == starter_audio.id
    assert not worker.folders[-1].exists()
    await _assert_confirmed_archive(worker, result, title)
