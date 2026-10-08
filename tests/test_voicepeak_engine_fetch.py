"""Official engine extraction: bounded archives and no existing-install overwrite."""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
from pathlib import Path
import stat
import threading
import warnings
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("voicepeak_engine_fetch_test", ROOT / "deploy" / "voicepeak-teto" / "termux" / "fetch-engine.py")
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


def linux_archive(extra=(), *, entries=None):
    out = io.BytesIO()
    items = entries if entries is not None else [
        ("Voicepeak/", b""),
        ("Voicepeak/voicepeak", b"test executable"),
        ("Voicepeak/fonts/test.otf", b"test font"),
        ("Voicepeak/dic/sys.dic", b"test dictionary"),
        *extra,
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
            for name, data in items:
                archive.writestr(name, data)
    return out.getvalue()


def outer_archive(tmp_path, monkeypatch, inner, *, duplicate=False):
    outer = tmp_path / "official.zip"
    monkeypatch.setattr(fetch, "EXPECTED_LINUX_SHA256", hashlib.sha256(inner).hexdigest())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(fetch.ARCHIVE_MEMBER, inner)
            if duplicate:
                archive.writestr(fetch.ARCHIVE_MEMBER, inner)
    return outer


def install(tmp_path, archive):
    return fetch.install_engine(archive=archive, destination=tmp_path / "installation", progress=lambda text: None)


def assert_no_partial(tmp_path):
    parent = tmp_path / "installation"
    assert not (parent / "Voicepeak").exists()
    assert not list(parent.glob(".voicepeak-extract-*"))


def test_preserves_full_layout_and_exec_permissions(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())
    tree = install(tmp_path, archive)
    assert tree == tmp_path / "installation" / "Voicepeak"
    assert (tree / "fonts" / "test.otf").read_bytes() == b"test font"
    assert (tree / "dic" / "sys.dic").read_bytes() == b"test dictionary"
    assert stat.S_IMODE((tree / "voicepeak").stat().st_mode) == 0o755
    assert stat.S_IMODE((tree / "fonts").stat().st_mode) == 0o755
    assert stat.S_IMODE((tree / "fonts" / "test.otf").stat().st_mode) == 0o644
    assert not list(tree.parent.glob(".voicepeak-extract-*"))


@pytest.mark.parametrize("existing", ["directory", "empty", "file", "broken_symlink"])
def test_refuses_any_existing_installation_before_download(tmp_path, monkeypatch, existing):
    parent = tmp_path / "installation"
    parent.mkdir()
    engine = parent / "Voicepeak"
    if existing in ("directory", "empty"):
        engine.mkdir()
        if existing == "directory":
            (engine / "usersettings").mkdir()
            (engine / "usersettings" / "license").write_text("keep existing activation")
    elif existing == "file":
        engine.write_text("keep this")
    else:
        engine.symlink_to(parent / "missing")
    monkeypatch.setattr(fetch, "_download_archive", lambda *args, **kwargs: pytest.fail("Must refuse before downloading"))
    with pytest.raises(fetch.InstallError, match="já existe"):
        fetch.install_engine(destination=parent)
    if existing == "directory":
        assert (engine / "usersettings" / "license").read_text() == "keep existing activation"
    assert not list(parent.glob(".voicepeak-extract-*"))


def test_repeated_install_keeps_existing_app_and_usersettings(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())
    tree = install(tmp_path, archive)
    (tree / "usersettings").mkdir()
    (tree / "usersettings" / "activation").write_text("existing")
    with pytest.raises(fetch.InstallError, match="já existe"):
        install(tmp_path, archive)
    assert (tree / "usersettings" / "activation").read_text() == "existing"


@pytest.mark.parametrize("unsafe", [
    "Voicepeak/../outside", "/Voicepeak/evil", "Voicepeak\\evil", "Elsewhere/evil",
    "Voicepeak/./evil", "Voicepeak//evil", "Voicepeak/voicepeak", "Voicepeak",
])
def test_rejects_unsafe_or_duplicate_paths_without_partial_install(tmp_path, monkeypatch, unsafe):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive([(unsafe, b"bad")]))
    with pytest.raises(fetch.InstallError):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFSOCK, stat.S_IFDIR])
def test_rejects_symlinks_special_files_and_mislabelled_directory(tmp_path, monkeypatch, kind):
    entry = zipfile.ZipInfo("Voicepeak/special")
    entry.create_system = 3
    entry.external_attr = (kind | 0o777) << 16
    archive = outer_archive(tmp_path, monkeypatch, linux_archive([(entry, b"bad")]))
    with pytest.raises(fetch.InstallError, match="especiais"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_refuses_file_used_as_directory(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive([("Voicepeak/occupied", b"file"), ("Voicepeak/occupied/child", b"child")]))
    with pytest.raises(fetch.InstallError, match="pasta"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_refuses_duplicate_outer_member(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive(), duplicate=True)
    with pytest.raises(fetch.InstallError, match="exatamente"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_refuses_wrong_sha_before_extracting(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())
    monkeypatch.setattr(fetch, "EXPECTED_LINUX_SHA256", "0" * 64)
    with pytest.raises(fetch.InstallError, match="SHA256"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_refuses_corrupt_crc_even_if_hash_is_accepted(tmp_path, monkeypatch):
    inner = linux_archive()
    assert inner.count(b"test dictionary") == 1
    inner = inner.replace(b"test dictionary", b"bad! dictionary")
    archive = outer_archive(tmp_path, monkeypatch, inner)
    with pytest.raises(fetch.InstallError, match="CRC"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


@pytest.mark.parametrize("limit_name,limit", [
    ("MAX_MEMBERS", 2), ("MAX_FILE_BYTES", 5), ("MAX_EXTRACTED_BYTES", 10),
    ("MAX_LINUX_ZIP_BYTES", 20), ("MAX_ARCHIVE_BYTES", 20),
])
def test_enforces_archive_entry_file_and_total_bounds(tmp_path, monkeypatch, limit_name, limit):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())
    monkeypatch.setattr(fetch, limit_name, limit)
    with pytest.raises(fetch.InstallError):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_incomplete_engine_refused(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive(entries=[("Voicepeak/voicepeak", b"alone")]))
    with pytest.raises(fetch.InstallError, match="incompleto"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_concurrent_empty_installation_is_never_overwritten(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())
    rename = fetch._rename_new

    def racing_install(source, destination):
        destination.mkdir()
        rename(source, destination)

    monkeypatch.setattr(fetch, "_rename_new", racing_install)
    with pytest.raises(fetch.InstallError, match="já existe"):
        install(tmp_path, archive)
    assert list((tmp_path / "installation" / "Voicepeak").iterdir()) == []
    assert not list((tmp_path / "installation").glob(".voicepeak-extract-*"))


def test_failure_before_atomic_commit_leaves_no_partial_install(tmp_path, monkeypatch):
    archive = outer_archive(tmp_path, monkeypatch, linux_archive())

    def fail_commit(*args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(fetch, "_rename_new", fail_commit)
    with pytest.raises(OSError, match="simulated"):
        install(tmp_path, archive)
    assert_no_partial(tmp_path)


def test_copy_rejects_stream_larger_than_metadata(tmp_path):
    with pytest.raises(fetch.InstallError, match="tamanho"):
        fetch._copy_stream(io.BytesIO(b"too big"), io.BytesIO(), limit=3, deadline=fetch.time.monotonic() + 5)


def test_copy_uses_total_deadline_and_remaining_socket_budget(monkeypatch):
    clock = [100.0]
    timeouts = []
    monkeypatch.setattr(fetch.time, "monotonic", lambda: clock[0])

    class SlowResponse:
        def read1(self, size):
            clock[0] += 0.6
            return b"x"

    with pytest.raises(fetch.InstallError, match="limite total"):
        fetch._copy_stream(SlowResponse(), io.BytesIO(), limit=100, deadline=101.0, set_timeout=timeouts.append)
    assert timeouts == pytest.approx([1.0, 0.4])


def test_download_rejects_changed_content_length_before_body(tmp_path, monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Length": "1"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read1(self, size): pytest.fail("Wrong size must be refused before body is read")

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == fetch.ARCHIVE_URL
            assert timeout <= fetch.SOCKET_TIMEOUT
            return Response()

    monkeypatch.setattr(fetch.urllib.request, "build_opener", lambda handler: Opener())
    with pytest.raises(fetch.InstallError, match="tamanho"):
        fetch._download_archive(tmp_path / "download.zip", deadline=fetch.time.monotonic() + 5, progress=lambda text: None)


def test_download_rejects_incomplete_body(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout): return Response(b"incomplete")

    monkeypatch.setattr(fetch.urllib.request, "build_opener", lambda handler: Opener())
    with pytest.raises(fetch.InstallError, match="incompleto"):
        fetch._download_archive(tmp_path / "download.zip", deadline=fetch.time.monotonic() + 5, progress=lambda text: None)


def test_complete_download_with_real_socket_handles_final_socket_close(tmp_path, monkeypatch):
    body = b"small test archive"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args): pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(fetch, "ARCHIVE_URL", "http://127.0.0.1:" + str(server.server_port) + "/archive.zip")
    monkeypatch.setattr(fetch, "EXPECTED_ARCHIVE_BYTES", len(body))
    target = tmp_path / "download.zip"
    try:
        fetch._download_archive(target, deadline=fetch.time.monotonic() + 5, progress=lambda text: None)
        assert target.read_bytes() == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_download_refuses_redirects():
    with pytest.raises(fetch.InstallError, match="redirecionamento"):
        fetch._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://other.example/engine.zip")


def test_help_does_not_download_or_execute_engine(monkeypatch, capsys):
    monkeypatch.setattr(fetch, "install_engine", lambda **kwargs: pytest.fail("Help must not install"))
    with pytest.raises(SystemExit) as result:
        fetch.main(["--help"])
    assert result.value.code == 0
    assert "não inclui voz ou licença" in " ".join(capsys.readouterr().out.split())
