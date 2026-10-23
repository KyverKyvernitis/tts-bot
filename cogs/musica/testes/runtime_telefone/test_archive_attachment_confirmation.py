"""Discord filename normalization must not strand otherwise valid archives."""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente.archive_manifest import (
    MANIFEST_FILENAME, inline_manifest, validate_manifest,
)
from cogs.musica.runtime_telefone.agente.archive_pipeline import ArchivePipelineMixin
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, _archive_metadata
from .test_arquivo_segmentado import FakeAttachment, FakeForum, FakeMessage, FakeThread, KEY, UploadWorker


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
@pytest.mark.parametrize("expected_name,wire_name,size", [
    ("Canção de teste-abcdef123456.ogg", "Canção de teste-abcdef123456.ogg", 100),
    ("Canção de teste-abcdef123456.ogg", "Canção_de_teste-abcdef123456.ogg", 100),
    ("The Cure - Boys Don't Cry-c7cd57487667.ogg", "The_Cure_-_Boys_Dont_Cry-c7cd57487667.ogg", 2656109),
    ("yungatita - 7 Weeks & 3 Days-d69901f6d4db.ogg", "yungatita_-_7_Weeks__3_Days-d69901f6d4db.ogg", 3395259),
])
async def test_attachment_confirmation_accepts_exact_and_observed_discord_names(expected_name, wire_name, size):
    raw = _raw_attachment(wire_name, size)
    result = await _confirmation_worker(raw)._archive_confirm_attachment(
        40, 30, filename=expected_name, size_bytes=size, bot_id=5,
    )
    assert result == raw["attachments"][0]
    assert result["filename"] == wire_name


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["size", "name", "case", "hash", "extra_separator", "punctuation", "author", "cdn", "ambiguous"])
async def test_attachment_confirmation_keeps_size_identity_and_ownership_checks(change, monkeypatch):
    raw = _raw_attachment("Canção_de_teste-abcdef123456.ogg")
    attachment = raw["attachments"][0]
    if change == "size":
        attachment["size"] = 99
    elif change == "name":
        attachment["filename"] = "outro-arquivo-abcdef123456.ogg"
    elif change == "case":
        attachment["filename"] = attachment["filename"].lower()
    elif change == "hash":
        attachment["filename"] = "Canção_de_teste-abcdef123457.ogg"
    elif change == "extra_separator":
        attachment["filename"] = "Canção__de_teste-abcdef123456.ogg"
    elif change == "punctuation":
        attachment["filename"] = "Canção-de_teste-abcdef123456.ogg"
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
        return FakeAttachment(self.next_attachment_id, _discord_filename(file.filename), file.fp.read())

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


def _discord_filename(filename):
    """Model the transformations observed in the user's authenticated replies."""
    return filename.replace("'", "").replace("&", "").replace(" ", "_")


async def _assert_confirmed_archive(worker, result, title, *, canonical_names=True):
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
        if canonical_names:
            assert part["filename"].isascii()
            assert part["filename"] == f"audio-{part['order'] + 1:06d}-{part['sha256']}.ogg"
            assert re.fullmatch(r"audio-\d{6}-[a-f0-9]{64}\.ogg", part["filename"])
        assert not any(name.startswith("_") for name in part)
        assert part["size_bytes"] == attachment.size
        assert part["sha256"] == hashlib.sha256(attachment.data).hexdigest()
        assert attachment.data == bytes([part["order"]]) * 100


@pytest.mark.asyncio
@pytest.mark.parametrize("title,segment_count", [
    ("Heaven Pierce Her - In the Presence of a King", 1),
    ("Kate Bush - Army Dreamers", 1),
    ("The Cure - Boys Don't Cry", 1),
    ("yungatita - 7 Weeks & 3 Days", 1),
    ("Canção com acentuação e underscore_preservado", 2),
    ("Cantor 🎵 · 'Faixa' & título / com caracteres", 2),
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
    assert re.fullmatch(r"audio-000001-[a-f0-9]{64}\.ogg", starter_audio.filename)
    worker._archive_confirm_attachment = confirm
    result = await worker._archive_one({**item, "retry": True})
    assert result["status"] == "done"
    assert worker.created == 1 and worker.sent_segments == 1
    assert worker.audio_uploads == 2 and worker.downloads == 1
    assert len(worker.forum.threads[0].messages) == 2
    assert worker.forum.threads[0].messages[0].attachments[0].id == starter_audio.id
    assert not worker.folders[-1].exists()
    await _assert_confirmed_archive(worker, result, title)


@pytest.mark.asyncio
async def test_canonical_retry_reuses_audio_after_display_title_changes(tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path)
    item = _item("Título original")
    worker.fail_manifest = True
    with pytest.raises(RuntimeError, match="interrompido"):
        await worker._archive_one(item)
    audio_ids = [message.attachments[0].id for message in worker.forum.threads[0].messages]
    new_title = "Título corrigido 🎵 com 'aspas' & pontuação"
    result = await worker._archive_one({**item, "retry": True,
                                       "track": {**item["track"], "title": new_title}})
    assert worker.created == 1 and worker.audio_uploads == 2 and worker.sent_segments == 1
    assert worker.downloads == 1
    assert [part["attachment_id"] for part in result["reference"]["segments"]] == audio_ids
    await _assert_confirmed_archive(worker, result, new_title)


async def _seed_legacy_incomplete_post(worker, item):
    """Create the pre-.88 wire layout without calling the current uploader."""
    from cogs.musica.runtime_telefone.agente.archive_staging import ArchiveWorkspace
    from cogs.musica.runtime_telefone.agente.validade_stream import _archive_audio_filename, _archive_url

    discord = ArchiveMixin._archive_one.__globals__["discord"]
    thread = FakeThread(worker)
    worker.forum.threads.append(thread)
    worker.created = 1
    title = item["track"]["title"]
    embed = discord.Embed(title=title, url=_archive_url(item["track"]["webpage_url"], KEY, version=8))
    embed.add_field(name="Fonte", value="🎵 YouTube")
    embed.add_field(name="Duração", value="13:20")
    embed.add_field(name="Formato", value="OPUS · 48 kHz · 2 canais")
    embed.add_field(name="Qualidade", value="≈192 kbps")
    embed.set_footer(text="Arquivo de músicas")
    safe_title = _archive_audio_filename(title, "")[:60]
    with ArchiveWorkspace(KEY, worker._archive_workspace_identity(item)) as staging:
        audio = await worker._archive_download(item, staging.path)
        parts = await worker._archive_prepare_segments(audio, staging.path)
        reference = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 0}
        for part in parts:
            filename = _archive_audio_filename(
                safe_title + (f" · {part['order'] + 1:06d}" if len(parts) > 1 else "")
                + "-" + part["sha256"][:12], part["path"].suffix,
            )
            worker.next_attachment_id += 1
            attachment = FakeAttachment(worker.next_attachment_id, _discord_filename(filename), part["path"].read_bytes())
            message = FakeMessage(worker, 30 + part["order"], thread,
                                  [embed] if part["order"] == 0 else [], [attachment])
            thread.messages.append(message)
            if part["order"] == 0:
                reference["attachment_id"] = attachment.id
        staging.save_reference(reference)
    worker.sent_segments = len(parts) - 1
    return reference, [message.attachments[0].id for message in thread.messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("title,segment_count", [
    ("The Cure - Boys Don't Cry", 1),
    ("yungatita - 7 Weeks & 3 Days", 1),
    ("Canção longa 'antiga' & partes", 2),
])
async def test_legacy_normalized_incomplete_post_preserves_existing_audio_ids(title, segment_count, tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path, segment_count=segment_count)
    item = _item(title)
    reference, audio_ids = await _seed_legacy_incomplete_post(worker, item)
    result = await worker._archive_one({**item, "retry": True, "existing_ref": reference})
    assert result["status"] == "done"
    assert worker.created == 1 and worker.audio_uploads == 0
    assert worker.sent_segments == segment_count - 1 and worker.downloads == 1
    assert [segment["attachment_id"] for segment in result["reference"]["segments"]] == audio_ids
    assert not worker.folders[-1].exists()
    await _assert_confirmed_archive(worker, result, title, canonical_names=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["manifest_upload", "final_manifest_ack"])
async def test_legacy_segmented_retry_after_manifest_interruption_reuses_all_audio(interruption, tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path)
    title = "Canção longa 'antiga' & partes"
    item = _item(title)
    reference, audio_ids = await _seed_legacy_incomplete_post(worker, item)
    retry_item = {**item, "retry": True, "existing_ref": reference}
    confirm = worker._archive_confirm_attachment
    if interruption == "manifest_upload":
        worker.fail_manifest = True
    else:
        async def lose_manifest_ack(channel_id, message_id, **kwargs):
            attachment = await confirm(channel_id, message_id, **kwargs)
            if kwargs["filename"] == MANIFEST_FILENAME:
                raise RuntimeError("ACK interrompido")
            return attachment
        worker._archive_confirm_attachment = lose_manifest_ack
    with pytest.raises(RuntimeError, match="interrompido"):
        await worker._archive_one(retry_item)
    assert worker.created == 1 and worker.audio_uploads == 0
    worker._archive_confirm_attachment = confirm
    result = await worker._archive_one(retry_item)
    assert worker.created == 1 and worker.audio_uploads == 0 and worker.sent_segments == 1
    assert worker.downloads == 1
    assert [part["attachment_id"] for part in result["reference"]["segments"]] == audio_ids
    await _assert_confirmed_archive(worker, result, title, canonical_names=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hash", "size", "case", "punctuation"])
async def test_legacy_alias_does_not_reuse_different_name_hash_or_size(change, tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path, segment_count=1)
    title = "The Cure - Boys Don't Cry"
    item = _item(title)
    reference, audio_ids = await _seed_legacy_incomplete_post(worker, item)
    root = worker.forum.threads[0].messages[0]
    old = root.attachments[0]
    if change == "hash":
        stem, extension = old.filename.rsplit(".", 1)
        old.filename = stem[:-1] + ("0" if stem[-1] != "0" else "1") + "." + extension
    elif change == "size":
        old.size -= 1
    elif change == "case":
        old.filename = old.filename.lower()
    else:
        old.filename = old.filename.replace("_", "-", 1)
    result = await worker._archive_one({**item, "retry": True, "existing_ref": reference})
    assert result["status"] == "done"
    assert worker.created == 1 and worker.audio_uploads == 1 and worker.downloads == 1
    assert result["reference"]["attachment_id"] not in audio_ids
    await _assert_confirmed_archive(worker, result, title)


@pytest.mark.asyncio
async def test_canonical_and_legacy_duplicate_candidates_are_rejected_before_upload(tmp_path, monkeypatch):
    worker = _prepare_upload(monkeypatch, tmp_path, segment_count=1)
    item = _item("The Cure - Boys Don't Cry")
    reference, _audio_ids = await _seed_legacy_incomplete_post(worker, item)
    root = worker.forum.threads[0].messages[0]
    old = root.attachments[0]
    digest = hashlib.sha256(old.data).hexdigest()
    root.attachments.append(FakeAttachment(99, f"audio-000001-{digest}.ogg", old.data))
    with pytest.raises(ValueError, match="mais de um anexo"):
        await worker._archive_one({**item, "retry": True, "existing_ref": reference})
    assert worker.created == 1 and worker.audio_uploads == 0
