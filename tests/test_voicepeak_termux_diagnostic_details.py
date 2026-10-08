"""Optional read-only details distinguish ldconfig stdout from exit status."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


TOOLKIT = Path(__file__).resolve().parents[1] / "deploy/voicepeak-teto/termux"
VERSION = "ldconfig (Ubuntu GLIBC 2.35-0ubuntu3.15) 2.35\nCopyright (C) GNU\n"
CACHE = ("2 libs found in cache `/etc/ld.so.cache'\n"
         "\tlibc.so.6 (libc6,x86-64) => /private/lib/libc.so.6\n"
         "\tlibm.so.6 (libc6,x86-64) => /private/lib/libm.so.6\n")


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("voicepeak_diagnostic_details", TOOLKIT / "diagnostic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("VOICEPEAK_TERMUX_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "login_command", lambda config, command, **options: ["guest", *command])
    return module


def fake_system(diagnostic, monkeypatch, *, wrapper_cache=CACHE, real_cache=CACHE, real_exists=0,
                wrapper_version=VERSION, real_version=VERSION):
    calls = []
    def execute(command, timeout, **options):
        calls.append((command, timeout, options))
        output = ""
        code = 0
        if command[-2:] == ["/usr/bin/dpkg", "--print-architecture"]:
            output = "amd64\n"
        elif "/usr/bin/dpkg-query" in command:
            output = "install ok installed\n"
        elif command[-3:] == ["/usr/bin/test", "-x", "/sbin/ldconfig.real"]:
            code = real_exists
        elif command[-2:] == ["/sbin/ldconfig", "--version"]:
            output = wrapper_version
        elif command[-2:] == ["/sbin/ldconfig.real", "--version"]:
            output = real_version
        elif command[-2:] == ["/sbin/ldconfig", "-p"]:
            output = wrapper_cache
        elif command[-2:] == ["/sbin/ldconfig.real", "-p"]:
            output = real_cache
        return {"ok": code == 0, "code": code, "output": output}
    monkeypatch.setattr(diagnostic, "bounded", execute)
    return calls


def test_details_compare_wrapper_and_real_without_exposing_paths(diagnostic, monkeypatch):
    calls = fake_system(diagnostic, monkeypatch)
    result = diagnostic.report(probe_system=True, probe_details=True)
    system = result["system_probe"]
    details = system["details"]
    assert details["read_only"] is True
    assert details["wrapper_cache"] == {"ok": True, "code": 0, "stdout_bytes": len(CACHE.encode()), "cache_header_count": 2, "library_entry_count": 2}
    assert details["wrapper_cache_no_seccomp"] == details["wrapper_cache"]
    assert details["wrapper_version"]["version_content_validated"] is True
    assert details["real_version"]["version_content_validated"] is True
    assert details["real_cache"]["cache_header_count"] == 2
    assert details["real_cache"]["library_entry_count"] == 2
    assert details["real_executable_present"] is True
    assert system["system_healthy"] is True and result["engine_verified"] is False
    assert "/private/lib/" not in json.dumps(result)
    assert VERSION not in json.dumps(result)
    for command, _, _ in calls:
        assert "--configure" not in command and "--list-narrator" not in command
        assert "/opt/Voicepeak/voicepeak" not in command
        if "/sbin/ldconfig.real" in command and "/usr/bin/test" not in command:
            assert command[-1] in {"-p", "--version"} or {"-N", "-X"}.issubset(command)


def test_wrapper_success_cannot_approve_empty_real_cache(diagnostic, monkeypatch):
    fake_system(diagnostic, monkeypatch, real_cache="", real_version="")
    system = diagnostic.report(probe_system=True, probe_details=True)["system_probe"]
    assert system["details"]["real_cache"] == {"ok": True, "code": 0, "stdout_bytes": 0, "cache_header_count": None, "library_entry_count": 0}
    assert system["details"]["wrapper_version"]["version_content_validated"] is True
    assert system["details"]["real_version"]["version_content_validated"] is False
    assert system["details"]["wrapper_cache"]["cache_header_count"] == 2
    assert system["system_healthy"] is False


@pytest.mark.parametrize("real_exists,present", [(1, False), (139, None)])
def test_missing_or_failed_real_executable_probe_skips_real_program(diagnostic, monkeypatch, real_exists, present):
    calls = fake_system(diagnostic, monkeypatch, real_exists=real_exists)
    details = diagnostic.report(probe_system=True, probe_details=True)["system_probe"]["details"]
    assert details["real_executable_present"] is present
    assert details["real_executable_probe"]["code"] == real_exists
    assert "real_version" not in details
    assert sum(command[-2:] == ["/sbin/ldconfig.real", "-p"] for command, _, _ in calls) == 2


def test_details_are_optional_and_do_not_add_default_guest_commands(diagnostic, monkeypatch):
    calls = fake_system(diagnostic, monkeypatch)
    result = diagnostic.report(probe_system=True)
    assert "details" not in result["system_probe"]
    assert len(calls) == 8
    assert not any("/sbin/ldconfig" in command or "--version" in command for command, _, _ in calls)


@pytest.mark.parametrize("output,count,entries", [
    ("", None, 0),
    ("0 libs found in cache `/etc/ld.so.cache'\n", 0, 0),
    ("unknown cache format\n", None, 0),
    ("2 libs found in cache `/etc/ld.so.cache'\n", 2, 0),
    ("\tlibc.so.6 (libc6,x86-64) => /lib/libc.so.6\n", None, 1),
    (CACHE, 2, 2),
    (CACHE.replace("\n", "\r\n"), 2, 2),
    ("9" * 5000 + " libs found in cache `/etc/ld.so.cache'\n", None, 0),
])
def test_cache_metadata_separates_empty_header_and_entries(diagnostic, output, count, entries):
    result = diagnostic.cache_details({"ok": True, "output": output})
    assert result == {"stdout_bytes": len(output.encode()), "cache_header_count": count, "library_entry_count": entries}


def test_metadata_uses_original_stdout_length_and_does_not_parse_failed_output(diagnostic):
    assert diagnostic.cache_details({"ok": True, "output": "", "stdout_bytes": 3})["stdout_bytes"] == 3
    assert diagnostic.cache_details({"ok": False, "output": CACHE}) == {"stdout_bytes": None, "cache_header_count": None, "library_entry_count": None}
    assert diagnostic.version_details({"ok": False, "output": VERSION}) == {"stdout_bytes": None, "version_content_validated": False}


@pytest.mark.parametrize("output,valid", [
    (VERSION, True), (VERSION.replace("\n", "\r\n"), True),
    ("ldconfig 2.35\n", True), ("", False),
    ("wrapper finished without running glibc\n", False),
    ("private license 2.35\n", False),
])
def test_version_requires_glibc_program_content(diagnostic, output, valid):
    assert diagnostic.version_details({"ok": True, "output": output})["version_content_validated"] is valid


def test_details_share_deadline_and_do_not_execute_after_expiry(diagnostic, monkeypatch):
    calls = fake_system(diagnostic, monkeypatch)
    moments = iter([100.0] + [100.1] * 8 + [103.0] * 4)
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: next(moments))
    details = diagnostic.report(probe_system=True, probe_details=True, timeout=2)["system_probe"]["details"]
    assert len(calls) == 8
    assert details["wrapper_version"]["error"] == "tempo total esgotado"
    assert details["wrapper_version"]["version_content_validated"] is False
    assert details["real_executable_probe"]["error"] == "tempo total esgotado"
    assert details["real_executable_present"] is None


@pytest.mark.parametrize("arguments", [["--probe-details"], ["--probe-details", "--probe-system", "--probe-runtime"]])
def test_cli_rejects_details_without_read_only_system_mode(diagnostic, monkeypatch, arguments):
    monkeypatch.setattr(diagnostic.sys, "argv", ["diagnostic.py", *arguments])
    monkeypatch.setattr(diagnostic, "report", lambda **options: pytest.fail("must reject before report"))
    with pytest.raises(SystemExit) as failure:
        diagnostic.main()
    assert failure.value.code == 2


def test_cli_runs_details_and_keeps_engine_unverified(diagnostic, monkeypatch, capsys):
    fake_system(diagnostic, monkeypatch)
    monkeypatch.setattr(diagnostic.sys, "argv", ["diagnostic.py", "--probe-system", "--probe-details"])
    assert diagnostic.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["system_probe"]["details"]["real_version"]["version_content_validated"] is True
    assert result["engine_verified"] is False


@pytest.mark.parametrize("options", [{"probe_details": True}, {"probe_system": True, "probe_details": True, "probe_runtime": True}])
def test_direct_report_rejects_details_outside_read_only_system_mode(diagnostic, options):
    with pytest.raises(ValueError, match="probe_details requer probe_system"):
        diagnostic.report(**options)
