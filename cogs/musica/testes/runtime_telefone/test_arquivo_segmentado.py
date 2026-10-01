from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente.archive_manifest import (
    MANIFEST_FILENAME, add_inline_manifest, encode_manifest, file_sha256,
    flatten_segments, hydrate_archive_manifest, inline_manifest, validate_manifest,
)
from cogs.musica.runtime_telefone.agente.archive_pipeline import upload_audio_budget
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, _archive_metadata, _archive_url

KEY = "c" * 32
REF = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}


def manifest_fixture():
    technical = {"audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2,
                 "audio_stream_index": 0, "audio_abr": 192}
    return {"version": 1, "archive_key": KEY, "duration": 800.0, "sha256": "a" * 64, **technical,
            "segments": [{"order": order, "offset_seconds": order * 400.0, "duration": 400.0,
                          "sha256": str(order) * 64, "size_bytes": 100, "filename": f"part-{order}.ogg", **technical,
                          "reference": {**REF, "message_id": 30 + order, "attachment_id": 9 + order}}
                         for order in range(2)]}


def test_manifest_accepts_long_tracks_and_rejects_foreign_or_invalid_segments():
    manifest = manifest_fixture()
    assert validate_manifest(manifest, archive_key=KEY, reference=REF)["duration"] == 800
    for mutate in (
        lambda value: value.update(version=99),
        lambda value: value["segments"][1]["reference"].update(channel_id=41),
        lambda value: value["segments"][1].update(offset_seconds=0),
        lambda value: value["segments"][1].update(sha256="invalid"),
        lambda value: value["segments"][1].update(duration=float("inf")),
    ):
        invalid = copy.deepcopy(manifest)
        mutate(invalid)
        with pytest.raises((ValueError, TypeError)):
            validate_manifest(invalid, archive_key=KEY, reference=REF)


@pytest.mark.asyncio
async def test_small_manifest_is_invisible_inline_and_legacy_posts_still_parse():
    discord = ArchiveMixin._archive_one.__globals__["discord"]
    embed = discord.Embed(url=_archive_url("https://youtube.com/watch?v=track", KEY, version=8))
    embed.add_field(name="Fonte", value="🎵 YouTube")
    embed.add_field(name="Duração", value="13:20")
    embed.add_field(name="Formato", value="OPUS · 48 kHz · 2 canais")
    embed.set_footer(text="Arquivo de músicas")
    manifest = manifest_fixture()
    add_inline_manifest(embed, manifest)
    assert all(not field.name.startswith("Manifesto ") for field in embed.fields)
    raw = {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40", "embeds": [embed.to_dict()],
           "attachments": [{"id": "9", "filename": "part-0.ogg", "size": 100,
                            "url": "https://cdn.discordapp.com/attachments/40/9/part-0.ogg"}]}
    assert (await hydrate_archive_manifest(raw))["_archive_manifest"] == manifest
    parsed = _archive_metadata(raw, KEY, 5, REF)
    assert parsed["duration"] == 800 and parsed["archive_segments"] == flatten_segments(manifest)
    for version in range(1, 8):
        legacy = copy.deepcopy(raw)
        legacy.pop("_archive_manifest", None)
        if version == 1:
            legacy["embeds"][0]["url"] = "https://youtube.com/watch?v=track"
            legacy["embeds"][0]["footer"]["text"] = "music-archive:v1:" + KEY
        else:
            legacy["embeds"][0]["url"] = _archive_url("https://youtube.com/watch?v=track", KEY, version=version)
        legacy["embeds"][0]["fields"] = [
            {"name": "Fonte", "value": "🎵 YouTube"}, {"name": "Duração", "value": "123:45"},
            {"name": "Formato", "value": "AAC · M4A · 44.1 kHz · 2 canais"}]
        if version == 7:
            legacy["embeds"][0]["description"] = "🎵 YouTube • 123:45\nAAC · M4A · 44.1 kHz · 2 canais • ≈192 kbps"
        reference = {**REF} if version >= 3 or version == 1 else {key: value for key, value in REF.items() if key != "forum_id"}
        result = _archive_metadata(legacy, KEY, 5, reference)
        assert result["duration"] == 7425 and result["audio_sample_rate"] == 44100


def test_audio_budget_uses_server_limit_and_cover_overhead():
    small = upload_audio_budget(SimpleNamespace(filesize_limit=5 * 1024 * 1024), cover_bytes=200000)
    large = upload_audio_budget(SimpleNamespace(filesize_limit=50 * 1024 * 1024), cover_bytes=200000)
    assert 4 * 1024 * 1024 < small < 5 * 1024 * 1024
    assert 45 * 1024 * 1024 < large < 50 * 1024 * 1024
    assert upload_audio_budget(SimpleNamespace(filesize_limit=5 * 1024 * 1024)) - small == 200000


@pytest.mark.asyncio
async def test_real_opus_segments_preserve_codec_and_all_sample_frames(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg indisponível")
    source = tmp_path / "original.ogg"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8.13",
                    "-ac", "2", "-c:a", "libopus", "-b:a", "192k", "-y", str(source)], check=True, capture_output=True)
    class Worker(ArchiveMixin):
        ffmpeg_executable = ffmpeg
        ffprobe_executable = ffprobe
    worker = Worker()
    ready = await worker._archive_audio_ready(source, tmp_path)
    segments = await worker._archive_prepare_segments(source, tmp_path, duration=ready[1], audio_index=0, codec="opus", budget=50000)
    assert len(segments) > 1
    def pcm_frames(path):
        result = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ar", "48000", "-ac", "2", "-f", "s16le", "-"], check=True, capture_output=True)
        return len(result.stdout) // 4
    assert sum(pcm_frames(part["path"]) for part in segments) == pcm_frames(source)
    assert all(part["audio_codec"] == "opus" and part["size_bytes"] <= 50000 for part in segments)
    assert sum(part["duration"] for part in segments) == pytest.approx(ready[1], abs=0.001)
    # Codec packets stay byte-identical. Only Ogg packaging and decoder preroll
    # change; the OpusHead tells FFmpeg to skip that duplicated preroll.
    import struct
    from cogs.musica.runtime_telefone.agente.archive_ogg import _packets, _packet_samples
    original_packets = list(_packets(source))[2:]
    recovered = []
    for order, part in enumerate(segments):
        packets = list(_packets(part["path"]))
        preskip = struct.unpack_from("<H", packets[0], 10)[0]
        audio = packets[2:]
        if order:
            assert preskip >= 3840
            while preskip:
                preskip -= _packet_samples(audio.pop(0))
            assert preskip == 0
        recovered.extend(audio)
    assert recovered == original_packets

    # A retry must reuse the same prepared bytes. New Ogg serial numbers would
    # change SHA-based filenames and cause duplicate child uploads.
    async def preparation_must_not_run(*args, **kwargs):
        raise AssertionError("validated staged segments should be reused")
    worker._archive_run_ffmpeg = preparation_must_not_run
    worker._archive_probe = preparation_must_not_run
    reused = await worker._archive_prepare_segments(source, tmp_path, duration=ready[1], audio_index=0, codec="opus", budget=50000)
    assert reused == segments
    assert [part["sha256"] for part in reused] == [part["sha256"] for part in segments]


class FakeAttachment:
    def __init__(self, attachment_id, filename, data):
        self.id, self.filename, self.data = attachment_id, filename, data
        self.size = len(data)
        self.url = f"https://cdn.discordapp.com/attachments/40/{self.id}/{filename}"
    async def save(self, destination):
        Path(destination).write_bytes(self.data)


class FakeMessage:
    def __init__(self, worker, message_id, channel, embeds=None, attachments=None):
        self.worker, self.id, self.channel = worker, message_id, channel
        self.author = SimpleNamespace(id=5)
        self.embeds, self.attachments = embeds or [], attachments or []
        self.content = ""
    async def edit(self, *, embed, attachments, allowed_mentions, content=None):
        if self.worker.fail_manifest and any(getattr(item, "filename", "") == MANIFEST_FILENAME and not hasattr(item, "id") for item in attachments):
            self.worker.fail_manifest = False
            raise RuntimeError("upload interrompido")
        self.embeds = [embed]
        self.attachments = [item if hasattr(item, "id") else self.worker.attachment(item) for item in attachments]
        return self


class FakeThread:
    id, name, owner_id, parent_id, archived = 40, "Long track", 5, 20, False
    def __init__(self, worker):
        self.worker, self.messages = worker, []
    async def fetch_message(self, message_id):
        return next(message for message in self.messages if message.id == message_id)
    async def history(self, **kwargs):
        messages = self.messages[:kwargs["limit"]] if kwargs.get("limit") else self.messages
        for message in messages:
            yield message
    async def send(self, *, content, embed, file, allowed_mentions):
        self.worker.sent_segments += 1
        message = FakeMessage(self.worker, 30 + len(self.messages), self, [embed], [self.worker.attachment(file)])
        message.content = content
        self.messages.append(message)
        return message


class FakeForum:
    id = 20
    def __init__(self, worker):
        self.worker = worker
        self.guild = SimpleNamespace(id=1, filesize_limit=5 * 1024 * 1024)
        self.flags = SimpleNamespace(require_tag=False)
        self.available_tags, self.threads = [], []
    def is_media(self):
        return False
    async def archived_threads(self, **kwargs):
        if False:
            yield None
    async def create_thread(self, *, name, content, embed, files, applied_tags, allowed_mentions):
        self.worker.created += 1
        thread = FakeThread(self.worker)
        self.threads.append(thread)
        message = FakeMessage(self.worker, 30, thread, [embed], [self.worker.attachment(file) for file in files])
        thread.messages.append(message)
        return SimpleNamespace(message=message, thread=thread)


class UploadWorker(ArchiveMixin):
    def __init__(self):
        self._archive_init()
        self.states, self.created, self.sent_segments, self.downloads = {}, 0, 0, 0
        self.fail_manifest, self.folders, self.next_attachment_id = False, [], 8
        self.forum = FakeForum(self)
        self.client = SimpleNamespace(user=SimpleNamespace(id=5), wait_until_ready=self.ready,
                                      get_channel=self.get_channel, http=SimpleNamespace(get_message=self.raw))
    async def ready(self):
        pass
    def get_channel(self, channel_id):
        if channel_id == 20:
            return self.forum
        if channel_id == 40 and self.forum.threads:
            return self.forum.threads[0]
    def attachment(self, file):
        self.next_attachment_id += 1
        file.fp.seek(0)
        return FakeAttachment(self.next_attachment_id, file.filename, file.fp.read())
    async def raw(self, channel_id, message_id):
        message = await self.get_channel(channel_id).fetch_message(message_id)
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": str(channel_id),
                "embeds": [embed.to_dict() for embed in message.embeds],
                "attachments": [{"id": str(item.id), "filename": item.filename, "size": item.size, "url": item.url}
                                for item in message.attachments]}
    async def _archive_wait_stable_voice(self):
        pass
    async def _archive_download(self, item, folder):
        self.folders.append(folder)
        path = folder / "audio.ogg"
        if not path.exists():
            self.downloads += 1
            path.write_bytes(b"sourceaudio")
        return path
    async def _archive_audio_ready(self, audio, folder):
        return audio, 800.0, 0, "opus"
    async def _archive_cover_for_track(self, track, folder):
        return None
    async def _archive_existing_cover(self, message, folder):
        return None
    async def _archive_prepare_segments(self, audio, folder, **kwargs):
        parts = []
        for order in range(2):
            path = folder / f"part-{order}.ogg"
            path.write_bytes(bytes([order]) * 100)
            parts.append({"order": order, "offset_seconds": order * 400.0, "duration": 400.0,
                          "size_bytes": 100, "sha256": file_sha256(path), "path": path,
                          "audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2,
                          "audio_stream_index": 0, "audio_abr": 192})
        return parts
    def log(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_upload_same_thread_retry_reuses_files_and_cleans_after_confirmation(tmp_path, monkeypatch):
    from cogs.musica.runtime_telefone.agente import archive_staging
    monkeypatch.setattr(archive_staging.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ArchiveMixin._archive_one.__globals__["discord"], "ForumChannel", FakeForum)
    worker = UploadWorker()
    item = {"key": KEY, "guild_id": 1, "channel_id": 20, "emoji": "🎵", "retry": False,
            "track": {"title": "Long track " * 30, "source": "YouTube", "duration": 800,
                      "webpage_url": "https://youtube.com/watch?v=long"}}
    worker.fail_manifest = True
    with pytest.raises(RuntimeError, match="interrompido"):
        await worker._archive_one(item)
    assert worker.created == 1 and worker.sent_segments == 1 and worker.folders[-1].is_dir()
    # Simulate restart without a catalog ACK; the staging ref recovers the post.
    result = await worker._archive_upload_v8(worker.forum, {**item, "retry": True})
    assert result["status"] == "done" and result["presentation"] == 8
    assert worker.created == 1 and worker.sent_segments == 1 and worker.downloads == 1
    assert len(result["reference"]["segments"]) == 2
    assert len({part["filename"] for part in result["reference"]["segments"]}) == 2
    assert not worker.folders[-1].exists()
    for segment in result["reference"]["segments"]:
        assert segment["channel_id"] == 40 and segment["forum_id"] == 20
    retry = await worker._archive_one({**item, "retry": True, "existing_ref": result["reference"]})
    assert retry["reference"] == result["reference"] and worker.created == 1
    assert len(worker.forum.threads[0].messages) == 2


@pytest.mark.asyncio
async def test_unknown_size_download_is_stopped_before_consuming_disk(tmp_path):
    from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveDownloadFailure
    stopped = asyncio.Event()
    class Process:
        returncode = None
        def kill(self):
            self.returncode = -9
            stopped.set()
        async def communicate(self):
            (tmp_path / "audio.ogg.part").write_bytes(b"x" * 200000)
            await stopped.wait()
            return b"", b""
    process = Process()
    with pytest.raises(ArchiveDownloadFailure, match="temporary_space_unavailable"):
        await ArchiveMixin()._archive_download_communicate(process, tmp_path, budget=100000, timeout=5)
    assert process.returncode == -9
    assert (tmp_path / "audio.ogg.part").is_file()  # partial bytes can be resumed


@pytest.mark.asyncio
async def test_resume_budget_includes_already_downloaded_bytes(tmp_path, monkeypatch):
    module = ArchiveMixin._archive_download.__globals__
    partial = tmp_path / "audio.ogg.part"
    partial.write_bytes(b"x" * 1024 * 1024)
    free_bytes = 200 * 1024 * 1024
    seen = []
    monkeypatch.setattr(module["shutil"], "disk_usage", lambda _path: SimpleNamespace(free=free_bytes))
    class Process:
        returncode = 0
        async def communicate(self):
            partial.rename(tmp_path / "audio.ogg")
            return b"", b""
    async def spawn(*args, **kwargs):
        seen.append(args)
        return Process()
    monkeypatch.setattr(module["asyncio"], "create_subprocess_exec", spawn)
    class Worker(ArchiveMixin):
        _archive_active = KEY
        states = {}
        def log(self, *args, **kwargs):
            pass
    item = {"key": KEY, "guild_id": 1, "emoji": "🎵", "track": {"duration": 3600, "webpage_url": "https://youtube.com/watch?v=long"}}
    result = await Worker()._archive_download(item, tmp_path)
    assert result.name == "audio.ogg"
    command = seen[0]
    budget = int(command[command.index("--max-filesize") + 1])
    assert budget == 1024 * 1024 + int(free_bytes * 0.45) - 64 * 1024 * 1024
    assert "--match-filter" not in command and "20M" not in command and "--no-part" not in command


@pytest.mark.asyncio
async def test_final_manifest_ack_loss_keeps_staging_until_fast_retry_confirmation(tmp_path, monkeypatch):
    from cogs.musica.runtime_telefone.agente import archive_staging
    monkeypatch.setattr(archive_staging.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ArchiveMixin._archive_one.__globals__["discord"], "ForumChannel", FakeForum)
    worker = UploadWorker()
    item = {"key": KEY, "guild_id": 1, "channel_id": 20, "emoji": "🎵", "retry": False,
            "track": {"title": "Long track", "source": "YouTube", "duration": 800,
                      "webpage_url": "https://youtube.com/watch?v=long"}}
    original_confirm = worker._archive_confirm_attachment
    async def interrupted_final_ack(channel_id, message_id, **kwargs):
        result = await original_confirm(channel_id, message_id, **kwargs)
        if kwargs["filename"] == MANIFEST_FILENAME:
            raise RuntimeError("ACK final perdido")
        return result
    worker._archive_confirm_attachment = interrupted_final_ack
    with pytest.raises(RuntimeError, match="ACK final"):
        await worker._archive_one(item)
    assert worker.folders[-1].exists()
    worker._archive_confirm_attachment = original_confirm
    result = await worker._archive_one({**item, "retry": True})
    assert result["status"] == "done" and not worker.folders[-1].exists()
    assert worker.created == 1 and worker.downloads == 1


def test_inline_manifest_rejects_compressed_bomb_and_duplicate_fragments():
    import base64
    import zlib
    from cogs.musica.runtime_telefone.agente.archive_manifest import INLINE_FRAGMENT_PREFIX
    bomb = base64.urlsafe_b64encode(zlib.compress(b" " * (8 * 1024 * 1024 + 1))).decode().rstrip("=")
    raw = {"embeds": [{"url": "https://youtube.com/#" + INLINE_FRAGMENT_PREFIX + bomb}]}
    with pytest.raises(ValueError):
        inline_manifest(raw)
    raw["embeds"][0]["url"] = "https://youtube.com/#" + INLINE_FRAGMENT_PREFIX + "abc&" + INLINE_FRAGMENT_PREFIX + "def"
    with pytest.raises(ValueError):
        inline_manifest(raw)


@pytest.mark.asyncio
async def test_vorbis_preparation_retry_preserves_container_bytes_without_ffmpeg(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg indisponível")
    source = tmp_path / "original.ogg"
    result = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.2:duration=4",
                             "-ac", "2", "-c:a", "libvorbis", "-q:a", "5", "-y", str(source)], capture_output=True)
    if result.returncode:
        pytest.skip("Encoder Vorbis indisponível")
    class Worker(ArchiveMixin):
        ffmpeg_executable = ffmpeg
        ffprobe_executable = ffprobe
    worker = Worker()
    ready = await worker._archive_audio_ready(source, tmp_path)
    segments = await worker._archive_prepare_segments(source, tmp_path, duration=ready[1], audio_index=0, codec="vorbis", budget=20000)
    assert len(segments) > 1
    async def must_not_prepare_again(*args, **kwargs):
        raise AssertionError("Vorbis retry must reuse staged container bytes")
    worker._archive_run_ffmpeg = must_not_prepare_again
    worker._archive_probe = must_not_prepare_again
    reused = await worker._archive_prepare_segments(source, tmp_path, duration=ready[1], audio_index=0, codec="vorbis", budget=20000)
    assert reused == segments
    assert [part["path"].read_bytes() for part in reused] == [part["path"].read_bytes() for part in segments]
