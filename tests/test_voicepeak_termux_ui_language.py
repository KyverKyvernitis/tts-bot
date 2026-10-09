"""Validate offline UI settings changes without a voice, activation or GUI."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from xml.dom import minidom

import pytest


HELPER = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux/set-ui-language.py"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("voicepeak_ui_language", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backups(path):
    return list(path.parent.glob(f"{path.name}.backup-*"))


def test_new_settings_uses_validated_native_schema_and_is_idempotent(helper, tmp_path):
    path = tmp_path / "engine/usersettings/settings/settings.xml"
    changed, backup = helper.set_ui_language(path)
    assert changed and backup is None
    document = minidom.parse(str(path))
    assert document.documentElement.tagName == "ApplicationSettings"
    interface = document.documentElement.getElementsByTagName("Interface")
    assert len(interface) == 1 and interface[0].getAttribute("language") == "english"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    assert helper.set_ui_language(path) == (False, None)
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert backups(path) == []


def test_update_preserves_other_settings_comments_and_exact_backup(helper, tmp_path):
    path = tmp_path / "settings.xml"
    original = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b'<!-- before root --><?keep instruction?>\n'
                b'<ApplicationSettings custom="unchanged">\n'
                b'  <Interface language="japanese" uiTextEditorFontSize="18" checkUpdateAtStartup="0">'
                b'<!-- inside interface --><Custom value="x &amp; y"/></Interface>\n'
                b'  <Paths openDir="/keep/this"/><UiStatus><Other/></UiStatus>\n'
                b'</ApplicationSettings><!-- after root -->\n')
    path.write_bytes(original)
    path.chmod(0o640)
    changed, backup = helper.set_ui_language(path)
    assert changed and backup in backups(path)
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    document = minidom.parse(str(path))
    root = document.documentElement
    interface = root.getElementsByTagName("Interface")[0]
    assert interface.getAttribute("language") == "english"
    assert interface.getAttribute("uiTextEditorFontSize") == "18"
    assert interface.getAttribute("checkUpdateAtStartup") == "0"
    assert root.getAttribute("custom") == "unchanged"
    assert root.getElementsByTagName("Paths")[0].getAttribute("openDir") == "/keep/this"
    assert interface.getElementsByTagName("Custom")[0].getAttribute("value") == "x & y"
    assert len(root.getElementsByTagName("Other")) == 1
    output = path.read_bytes()
    assert all(comment in output for comment in [b"<!-- before root -->", b"<!-- inside interface -->", b"<!-- after root -->"])
    assert b"<?keep instruction?>" in output
    current = (output, path.stat().st_ino, path.stat().st_mtime_ns)
    assert helper.set_ui_language(path) == (False, None)
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == current
    assert backups(path) == [backup]


def test_missing_interface_added_and_japanese_supported(helper, tmp_path):
    path = tmp_path / "settings.xml"
    original = b'<ApplicationSettings><Audio audioSystemName="ALSA"/></ApplicationSettings>'
    path.write_bytes(original)
    _, backup = helper.set_ui_language(path, "japanese")
    document = minidom.parse(str(path))
    assert document.getElementsByTagName("Interface")[0].getAttribute("language") == "japanese"
    assert document.getElementsByTagName("Audio")[0].getAttribute("audioSystemName") == "ALSA"
    assert backup.read_bytes() == original


@pytest.mark.parametrize("source", [
    b"", b"not XML", b"<ApplicationSettings>", b"<Other><Interface/></Other>",
    b"<ApplicationSettings><Interface/><Interface language='english'/></ApplicationSettings>",
    b"<ApplicationSettings><Interface language='english' language='japanese'/></ApplicationSettings>",
    b'<!DOCTYPE ApplicationSettings><ApplicationSettings/>',
    b'<!DOCTYPE ApplicationSettings SYSTEM "https://example.invalid/external.dtd"><ApplicationSettings/>',
    b'<!DOCTYPE ApplicationSettings [<!ENTITY x "expanded">]><ApplicationSettings>&x;</ApplicationSettings>',
    '<!DOCTYPE ApplicationSettings [<!ENTITY x "expanded">]><ApplicationSettings>&x;</ApplicationSettings>'.encode("utf-16"),
    b"<ApplicationSettings/>" + b" " * (1024 * 1024),
])
def test_invalid_or_unsafe_xml_preserved_without_backup(helper, tmp_path, source):
    path = tmp_path / "settings.xml"
    path.write_bytes(source)
    before = (path.stat().st_ino, path.stat().st_mtime_ns)
    with pytest.raises(helper.LanguageSettingError):
        helper.set_ui_language(path)
    assert path.read_bytes() == source
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.xml"]


def test_utf16_xml_preserves_unicode_when_changing_language(helper, tmp_path):
    path = tmp_path / "settings.xml"
    original = '<?xml version="1.0" encoding="UTF-16"?><ApplicationSettings note="日本語"><Interface language="japanese"/></ApplicationSettings>'.encode("utf-16")
    path.write_bytes(original)
    _, backup = helper.set_ui_language(path)
    assert backup.read_bytes() == original
    document = minidom.parse(str(path))
    assert document.documentElement.getAttribute("note") == "日本語"
    assert document.getElementsByTagName("Interface")[0].getAttribute("language") == "english"


def test_symlink_file_does_not_modify_target(helper, tmp_path):
    target = tmp_path / "real.xml"
    original = b'<ApplicationSettings><Interface language="japanese"/></ApplicationSettings>'
    target.write_bytes(original)
    path = tmp_path / "settings.xml"
    path.symlink_to(target)
    with pytest.raises(helper.LanguageSettingError, match="regular"):
        helper.set_ui_language(path)
    assert path.is_symlink() and target.read_bytes() == original
    assert backups(path) == []


def test_symlink_parent_does_not_create_or_write_target(helper, tmp_path):
    target = tmp_path / "real-directory"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        helper.set_ui_language(alias / "new-directory/settings.xml")
    assert list(target.iterdir()) == []


def test_traverse_only_android_ancestors_need_no_read_permission(helper, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory read permissions")
    ancestor = tmp_path / "data"
    engine = ancestor / "data/app/engine"
    engine.mkdir(parents=True)
    restricted = [ancestor, ancestor / "data", ancestor / "data/app"]
    for directory in restricted:
        directory.chmod(0o111)
    try:
        with pytest.raises(PermissionError):
            os.listdir(ancestor)
        path = engine / "usersettings/settings/settings.xml"
        assert helper.set_ui_language(path) == (True, None)
        assert helper.set_ui_language(path) == (False, None)
        assert 'language="english"' in path.read_text()
    finally:
        for directory in restricted:
            directory.chmod(0o700)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_settings_refused_without_blocking(helper, tmp_path, kind):
    path = tmp_path / "settings.xml"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(helper.LanguageSettingError, match="regular"):
        helper.set_ui_language(path)
    assert backups(path) == []


def test_source_changed_before_commit_preserves_new_source(helper, tmp_path, monkeypatch):
    path = tmp_path / "settings.xml"
    path.write_bytes(b'<ApplicationSettings><Interface language="japanese"/></ApplicationSettings>')
    competing = b'<ApplicationSettings><Interface language="japanese"/><NewSetting value="preserve"/></ApplicationSettings>'
    verify = helper._verify_source

    def change_source(parent, filename, source):
        path.write_bytes(competing)
        verify(parent, filename, source)

    monkeypatch.setattr(helper, "_verify_source", change_source)
    with pytest.raises(helper.LanguageSettingError, match="mudou"):
        helper.set_ui_language(path)
    assert path.read_bytes() == competing
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.xml"]


def test_source_changed_after_backup_preserves_new_source_and_cleans_staging(helper, tmp_path, monkeypatch):
    path = tmp_path / "settings.xml"
    path.write_bytes(b'<ApplicationSettings><Interface language="japanese"/></ApplicationSettings>')
    competing = b'<ApplicationSettings><NewSetting/></ApplicationSettings>'
    verify = helper._verify_source
    calls = []

    def change_source(parent, filename, source):
        calls.append(True)
        if len(calls) == 2:
            path.write_bytes(competing)
        verify(parent, filename, source)

    monkeypatch.setattr(helper, "_verify_source", change_source)
    with pytest.raises(helper.LanguageSettingError, match="mudou"):
        helper.set_ui_language(path)
    assert path.read_bytes() == competing
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.xml"]


def test_failed_atomic_replace_preserves_original_and_removes_staging(helper, tmp_path, monkeypatch):
    path = tmp_path / "settings.xml"
    original = b'<ApplicationSettings><Interface language="japanese"/></ApplicationSettings>'
    path.write_bytes(original)

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(helper.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        helper.set_ui_language(path)
    assert path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.xml"]


@pytest.mark.parametrize("collision_name", [".settings.xml.ui-language-fixed.tmp", "settings.xml.backup-fixed-fixed"])
def test_exclusive_staging_or_backup_collision_preserves_preexisting_file(helper, tmp_path, monkeypatch, collision_name):
    path = tmp_path / "settings.xml"
    original = b'<ApplicationSettings><Interface language="japanese"/></ApplicationSettings>'
    path.write_bytes(original)
    collision = tmp_path / collision_name
    collision.write_bytes(b"preexisting file must survive")
    monkeypatch.setattr(helper.secrets, "token_hex", lambda _count: "fixed")
    monkeypatch.setattr(helper.time, "strftime", lambda *_args: "fixed")
    with pytest.raises(FileExistsError):
        helper.set_ui_language(path)
    assert path.read_bytes() == original
    assert collision.read_bytes() == b"preexisting file must survive"
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted(["settings.xml", collision_name])


def test_new_source_created_during_publication_is_not_replaced(helper, tmp_path, monkeypatch):
    path = tmp_path / "settings.xml"
    competing = b'<ApplicationSettings><CreatedByAnotherWriter/></ApplicationSettings>'
    link = helper.os.link

    def competing_link(*args, **kwargs):
        path.write_bytes(competing)
        return link(*args, **kwargs)

    monkeypatch.setattr(helper.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        helper.set_ui_language(path)
    assert path.read_bytes() == competing
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.xml"]


def make_config(path, engine):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "container": "voicepeak-arm64", "backend": "box64",
        "guest_executable": "/opt/Voicepeak/voicepeak", "engine_directory": str(engine), "display": "",
    }))


def test_default_path_prefers_corrected_config_and_restores_environment(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VOICEPEAK_TERMUX_CONFIG", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    base_engine = tmp_path / "base-engine"
    corrected_engine = tmp_path / "corrected-engine"
    base_engine.mkdir()
    corrected_engine.mkdir()
    directory = tmp_path / ".voicepeak-termux"
    make_config(directory / "config-box64.json", base_engine)
    assert helper.settings_path() == base_engine / "usersettings/settings/settings.xml"
    make_config(directory / "config-box64-x11fix.json", corrected_engine)
    assert helper.settings_path() == corrected_engine / "usersettings/settings/settings.xml"
    assert "VOICEPEAK_TERMUX_CONFIG" not in os.environ


def test_explicit_config_is_honored_and_preserved(helper, tmp_path, monkeypatch):
    engine = tmp_path / "explicit-engine"
    engine.mkdir()
    config = tmp_path / "custom.json"
    make_config(config, engine)
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(config))
    monkeypatch.delenv("DISPLAY", raising=False)
    assert helper.settings_path() == engine / "usersettings/settings/settings.xml"
    assert os.environ["VOICEPEAK_TERMUX_CONFIG"] == str(config)


def test_invalid_corrected_config_is_not_silently_replaced_with_base(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VOICEPEAK_TERMUX_CONFIG", raising=False)
    engine = tmp_path / "engine"
    engine.mkdir()
    directory = tmp_path / ".voicepeak-termux"
    make_config(directory / "config-box64.json", engine)
    (directory / "config-box64-x11fix.json").write_text("not JSON")
    with pytest.raises(helper.LanguageSettingError):
        helper.settings_path()
    assert "VOICEPEAK_TERMUX_CONFIG" not in os.environ
    assert list(engine.iterdir()) == []


def test_isolated_help_and_explicit_settings_need_no_runtime_or_launcher(tmp_path):
    standalone = tmp_path / "set-ui-language.py"
    standalone.write_bytes(HELPER.read_bytes())
    environment = dict(os.environ)
    environment["VOICEPEAK_TERMUX_CONFIG"] = str(tmp_path / "missing-config.json")
    help_result = subprocess.run([sys.executable, "-I", str(standalone), "--help"],
                                 env=environment, capture_output=True, text=True, timeout=5)
    assert help_result.returncode == 0 and "Feche o VOICEPEAK" in help_result.stdout
    path = tmp_path / "standalone-settings.xml"
    result = subprocess.run([sys.executable, "-I", str(standalone), "--settings", str(path)],
                            env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'language="english"' in path.read_text()
    assert not (tmp_path / "missing-config.json").exists()


def test_unsupported_language_rejected_before_creating_directories(helper, tmp_path):
    with pytest.raises(helper.LanguageSettingError, match="idioma"):
        helper.set_ui_language(tmp_path / "missing/settings.xml", "portuguese")
    assert list(tmp_path.iterdir()) == []
