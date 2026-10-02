import asyncio
import io
import os
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from utility import base_archive as module


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "main.py"], check=True)
    return root


def add(repo, relative, data="CODE = True\n"):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    subprocess.run(["git", "-C", str(repo), "add", "--", relative], check=True)
    return path


def members(result):
    with zipfile.ZipFile(io.BytesIO(result.payload)) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def test_dirty_working_tree_only_and_root_from_subdirectory(repo, monkeypatch):
    add(repo, "cogs/music/file.py")
    (repo / "main.py").write_text("VALUE = 2\n")
    (repo / "untracked.py").write_text("EXCLUDED = True\n")
    monkeypatch.setattr(module, "archive_filename", lambda: "repo-test.zip")
    result = module.build_git_tracked_base_archive_sync(repo / "cogs/music")
    assert result.filename == "repo-test.zip"
    assert result.file_count == 2
    assert members(result) == {
        "tts-bot-main/main.py": b"VALUE = 2\n",
        "tts-bot-main/cogs/music/file.py": b"CODE = True\n",
    }


@pytest.mark.parametrize("relative", [
    ".env", ".env.local", "secret.pem", "secrets.key", "db.sqlite3", "run.log",
    "cookies-test.txt", "photo.png", "song.opus", "bundle.jar", "program",
    "assets/code.py", "audio/source.py", "build/code.py", "dist/code.py", "manifest.json",
    "android/core-worker-app/releases/source.py",
])
def test_sensitive_assets_binary_and_generated_exclusions(repo, relative):
    # An extensionless executable is not classified by filename; use a binary folder.
    if relative == "program":
        relative = "android/core-worker-app/app/src/main/assets/core-linux/bin/program"
    add(repo, relative)
    assert set(members(module.build_git_tracked_base_archive_sync(repo))) == {"tts-bot-main/main.py"}


@pytest.mark.parametrize("relative", [".env.example", ".env.sample", ".env.template"])
def test_env_examples_and_source_contents_are_preserved(repo, relative):
    add(repo, relative, "TOKEN=example-placeholder\n")
    result = module.build_git_tracked_base_archive_sync(repo)
    assert members(result)["tts-bot-main/" + relative] == b"TOKEN=example-placeholder\n"


def test_path_exclusions_run_before_file_stat(repo, monkeypatch):
    add(repo, "photo.png")
    original = Path.lstat

    def verify(path, *args, **kwargs):
        assert path.name != "photo.png", "excluded asset should not be inspected"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", verify)
    assert module.build_git_tracked_base_archive_sync(repo).file_count == 1


def test_large_files_and_deleted_tracked_files_are_excluded(repo, monkeypatch):
    monkeypatch.setattr(module, "BASE_ARCHIVE_MAX_FILE_BYTES", 32)
    add(repo, "large.py", "a" * 33)
    (repo / "main.py").unlink()
    add(repo, "small.py", "ok\n")
    assert members(module.build_git_tracked_base_archive_sync(repo)) == {"tts-bot-main/small.py": b"ok\n"}


def test_symlinks_in_files_and_ancestors_are_rejected(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.py").write_text("PRIVATE = True\n")
    add(repo, "nested/data.py")
    (repo / "nested/data.py").unlink()
    (repo / "nested").rmdir()
    (repo / "nested").symlink_to(outside, target_is_directory=True)
    (repo / "link.py").symlink_to(outside / "data.py")
    subprocess.run(["git", "-C", str(repo), "add", "link.py"], check=True)
    assert members(module.build_git_tracked_base_archive_sync(repo)) == {"tts-bot-main/main.py": b"VALUE = 1\n"}


def test_symlink_replacement_after_snapshot_never_reads_external_contents(repo, tmp_path, monkeypatch):
    outside = tmp_path / "private.py"
    outside.write_text("PRIVATE = True\n")
    original = module._read_source
    replaced = False

    def replace_source(root_fd, source, deadline):
        nonlocal replaced
        if source.relative == "main.py" and not replaced:
            (repo / "main.py").unlink()
            (repo / "main.py").symlink_to(outside)
            replaced = True
        return original(root_fd, source, deadline)

    add(repo, "still-safe.py")
    monkeypatch.setattr(module, "_read_source", replace_source)
    result = module.build_git_tracked_base_archive_sync(repo)
    assert members(result) == {"tts-bot-main/still-safe.py": b"CODE = True\n"}


def test_git_errors_do_not_expose_raw_stderr(tmp_path, monkeypatch):
    def failure(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "TOKEN=private-placeholder")

    monkeypatch.setattr(module.subprocess, "run", failure)
    with pytest.raises(module.BaseArchiveError) as error:
        module.build_git_tracked_base_archive_sync(tmp_path)
    assert "private-placeholder" not in str(error.value)


def test_missing_git_or_no_eligible_files_have_clear_errors(repo, tmp_path, monkeypatch):
    with pytest.raises(module.BaseArchiveError, match="Git"):
        module.build_git_tracked_base_archive_sync(tmp_path)
    (repo / "main.py").unlink()
    with pytest.raises(module.BaseArchiveError, match="elegível"):
        module.build_git_tracked_base_archive_sync(repo)


def test_total_archive_limit_includes_central_directory(repo, monkeypatch):
    monkeypatch.setattr(module, "BASE_ARCHIVE_MAX_BYTES", 60)
    with pytest.raises(module.BaseArchiveError, match="grande demais"):
        module.build_git_tracked_base_archive_sync(repo)


def test_generation_retries_changed_files_once(repo, monkeypatch):
    original = module._write_archive
    calls = 0

    def write(snapshot, deadline):
        nonlocal calls
        calls += 1
        result = original(snapshot, deadline)
        if calls == 1:
            (repo / "main.py").write_text("VALUE = 3\n")
        return result

    monkeypatch.setattr(module, "_write_archive", write)
    assert members(module.build_git_tracked_base_archive_sync(repo))["tts-bot-main/main.py"] == b"VALUE = 3\n"
    assert calls == 2


def test_continuously_changing_files_abort_after_two_attempts(repo, monkeypatch):
    original = module._write_archive
    calls = 0

    def write(snapshot, deadline):
        nonlocal calls
        calls += 1
        result = original(snapshot, deadline)
        (repo / "main.py").write_text(f"VALUE = {calls + 1}\n")
        return result

    monkeypatch.setattr(module, "_write_archive", write)
    with pytest.raises(module.BaseArchiveError, match="mudou"):
        module.build_git_tracked_base_archive_sync(repo)
    assert calls == 2


def test_deadline_and_git_timeout_are_bounded(repo, monkeypatch):
    with pytest.raises(module.BaseArchiveTimeout):
        module.build_git_tracked_base_archive_sync(repo, timeout=0)
    seen = []

    def timeout(*args, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(module.BaseArchiveTimeout):
        module.build_git_tracked_base_archive_sync(repo, timeout=0.5)
    assert 0 < seen[0] <= 0.5


@pytest.mark.asyncio
async def test_cache_invalidates_same_size_edits_tracked_membership_and_policy(repo, monkeypatch):
    service = module.BaseArchiveService(repo)
    first = await service.get_archive()
    reused = await service.get_archive()
    assert not first.cache_hit and reused.cache_hit
    assert first.payload is reused.payload

    source = repo / "main.py"
    old = source.stat()
    source.write_text("VALUE = 4\n")
    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    edited = await service.get_archive()
    assert not edited.cache_hit
    assert members(edited)["tts-bot-main/main.py"] == b"VALUE = 4\n"

    add(repo, "extra.py", "MORE = 1\n")
    added = await service.get_archive()
    assert not added.cache_hit and added.file_count == 2
    subprocess.run(["git", "-C", str(repo), "rm", "--cached", "-q", "extra.py"], check=True)
    removed = await service.get_archive()
    assert not removed.cache_hit and removed.file_count == 1

    monkeypatch.setattr(module, "BASE_ARCHIVE_POLICY_VERSION", module.BASE_ARCHIVE_POLICY_VERSION + 1)
    assert not (await service.get_archive()).cache_hit


@pytest.mark.asyncio
async def test_checkout_and_deletion_invalidate_cache(repo):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "-m", "initial"], check=True)
    service = module.BaseArchiveService(repo)
    initial = await service.get_archive()
    (repo / "main.py").write_text("VALUE = 5\n")
    assert not (await service.get_archive()).cache_hit
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "main.py"], check=True)
    restored = await service.get_archive()
    assert not restored.cache_hit
    assert members(restored) == members(initial)
    add(repo, "kept.py")
    await service.get_archive()
    (repo / "main.py").unlink()
    deleted = await service.get_archive()
    assert not deleted.cache_hit and deleted.file_count == 1


@pytest.mark.asyncio
async def test_cache_expiry_releases_artifact_and_completed_task(repo):
    service = module.BaseArchiveService(repo, cache_ttl=0.03)
    await service.get_archive()
    assert service._cache is not None
    await asyncio.sleep(0.06)
    assert service._cache is None
    assert service._expiry_handle is None
    assert service._inflight is None
    assert not (await service.get_archive()).cache_hit


@pytest.mark.asyncio
async def test_concurrent_waiters_timeout_and_cancellation_share_one_worker(repo, monkeypatch):
    original = module._write_archive
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked(snapshot, deadline):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return original(snapshot, deadline)

    monkeypatch.setattr(module, "_write_archive", blocked)
    service = module.BaseArchiveService(repo)
    short = asyncio.create_task(service.get_archive(wait_timeout=0.03))
    cancelled = asyncio.create_task(service.get_archive(wait_timeout=2))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(module.BaseArchiveTimeout):
            await short
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        waiters = [asyncio.create_task(service.get_archive(wait_timeout=2)) for _ in range(6)]
        await asyncio.sleep(0)
        assert calls == 1 and service._inflight is not None
        release.set()
        results = await asyncio.gather(*waiters)
        assert all(item is results[0] for item in results)
        assert calls == 1
        assert (await service.get_archive()).cache_hit
    finally:
        release.set()


@pytest.mark.asyncio
async def test_failed_worker_is_retryable_and_does_not_cache_failure(repo, monkeypatch):
    original = module._write_archive
    calls = 0

    def fail_once(snapshot, deadline):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("test failure")
        return original(snapshot, deadline)

    monkeypatch.setattr(module, "_write_archive", fail_once)
    service = module.BaseArchiveService(repo)
    with pytest.raises(module.BaseArchiveError, match="preparar"):
        await service.get_archive()
    assert service._cache is None
    assert (await service.get_archive()).file_count == 1
