"""Configuration facade contracts, independent of the implementation location."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"


def load_worker(path=PHONE):
    spec = importlib.util.spec_from_file_location("phone_config_test", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


@pytest.mark.parametrize("key", ["", None, "lower", "A-B", "export KEY", "A=1", "A\nB", "A;B"])
def test_rejects_invalid_keys_without_touching_existing_file(tmp_path, key):
    worker = load_worker()
    path = tmp_path / "config.env"
    path.write_text("ORIGINAL=yes\n")
    with pytest.raises(ValueError, match="chave de env inválida"):
        worker._update_env_file(str(path), {key: "new"})
    assert path.read_text() == "ORIGINAL=yes\n"


def test_merge_preserves_comments_duplicates_and_late_facade_bindings(tmp_path, monkeypatch):
    worker = load_worker()
    path = tmp_path / "config.env"
    path.write_text("# keep\nexport PHONE_CONFIG_TEST=old\nUNRELATED=stay\nPHONE_CONFIG_TEST=duplicate\n")
    monkeypatch.setenv("PHONE_CONFIG_TEST", "before")
    monkeypatch.setenv("PHONE_CONFIG_NEW", "before")
    # Reassign after the formatter has already been used/loaded. The writer
    # must resolve these facade bindings at call time, including in a domain.
    worker._format_env_value("warmup")
    monkeypatch.setattr(worker, "_safe_env_key", lambda key: str(key).strip().upper())
    seen = []
    monkeypatch.setattr(worker, "_format_env_value", lambda value: seen.append(value) or json.dumps(value, ensure_ascii=False))
    updates = {" phone_config_test ": "A B", "phone_config_new": "olá"}
    assert worker._update_env_file(str(path), updates) == path
    assert path.read_text() == ('# keep\nPHONE_CONFIG_TEST="A B"\nUNRELATED=stay\nPHONE_CONFIG_TEST="A B"\n\n'
        '# Core Worker pareado automaticamente. Não envie estes valores ao GitHub.\nPHONE_CONFIG_NEW="olá"\n')
    assert seen == ["A B", "A B", "olá"]
    assert os.environ["PHONE_CONFIG_TEST"] == "A B"
    assert os.environ["PHONE_CONFIG_NEW"] == "olá"
    assert path.stat().st_mode & 0o777 == 0o600
    assert updates == {" phone_config_test ": "A B", "phone_config_new": "olá"}


@pytest.mark.parametrize("value", ["plain-42/@:a", "olá com espaços", '"quoted"', "d'água",
                                 "$PHONE_CONFIG_PROBE", "prefix;PHONE_CONFIG_PROBE=changed",
                                 "`printf changed`", r"literal\path$PHONE_CONFIG_PROBE"])
def test_generated_values_are_literal_in_shell_and_both_python_readers(tmp_path, monkeypatch, value):
    worker = load_worker()
    path = tmp_path / "config.env"
    monkeypatch.setenv("PHONE_CONFIG_VALUE", "before")
    monkeypatch.setenv("PHONE_CONFIG_PROBE", "unchanged")
    worker._update_env_file(str(path), {"PHONE_CONFIG_VALUE": value})
    # Run only a local shell with controlled probe strings. The file is the
    # output of the production writer, as sourced by the existing start scripts.
    result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s\\0%s" "$PHONE_CONFIG_VALUE" "$PHONE_CONFIG_PROBE"',
                             "config-test", str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0") == [value, "unchanged"]
    monkeypatch.delenv("PHONE_CONFIG_VALUE")
    worker._load_env_file(str(path))
    assert os.environ["PHONE_CONFIG_VALUE"] == value
    monkeypatch.delenv("PHONE_CONFIG_VALUE")
    worker._load_env_file_once(path)
    assert os.environ["PHONE_CONFIG_VALUE"] == value


def test_existing_json_config_keeps_escapes_and_export_precedence(tmp_path, monkeypatch):
    worker = load_worker()
    path = tmp_path / "old.env"
    value = 'line\n"quoted"\\path$literal'
    path.write_text('export PHONE_CONFIG_VALUE=' + json.dumps(value) + '\nPHONE_CONFIG_EXISTING=file\n')
    monkeypatch.delenv("PHONE_CONFIG_VALUE", raising=False)
    monkeypatch.setenv("PHONE_CONFIG_EXISTING", "exported")
    worker._load_env_file(str(path))
    assert os.environ["PHONE_CONFIG_VALUE"] == value
    assert os.environ["PHONE_CONFIG_EXISTING"] == "exported"


def test_config_loading_is_lazy_shared_and_resolves_module_bindings(monkeypatch):
    worker = load_worker()
    assert worker._PHONE_WORKER_CONFIG_MODULE is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: worker._phone_worker_config_module(), range(32)))
    assert all(item is modules[0] for item in modules)
    assert Path(modules[0].__file__) == PHONE.parent / "phone_worker_runtime/config.py"
    monkeypatch.setattr(modules[0], "format_env_value", lambda value: "patched:" + value)
    assert worker._format_env_value("late") == "patched:late"


def test_lone_entrypoint_can_load_config_before_modules_arrive(tmp_path, monkeypatch):
    bare = tmp_path / "phone_worker.py"
    bare.write_bytes(PHONE.read_bytes())
    env = tmp_path / "phone.env"
    env.write_text('PHONE_CONFIG_VALUE="standalone"\n')
    monkeypatch.delenv("PHONE_CONFIG_VALUE", raising=False)
    worker = load_worker(bare)
    worker._load_env_file_once(env)
    assert os.environ["PHONE_CONFIG_VALUE"] == "standalone"
    assert worker._PHONE_WORKER_CONFIG_MODULE is None
    with pytest.raises(RuntimeError, match="segundo estágio"):
        worker._safe_env_key("PHONE_CONFIG_VALUE")
    assert worker._PHONE_WORKER_CONFIG_MODULE is None
    shutil.copytree(PHONE.parent / "phone_worker_runtime", tmp_path / "phone_worker_runtime",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert worker._safe_env_key(" PHONE_CONFIG_VALUE ") == "PHONE_CONFIG_VALUE"
    assert worker._phone_worker_config_module().__file__.startswith(str(tmp_path))


def test_pure_config_import_and_render_have_no_io_or_runtime_side_effects(tmp_path):
    code = r'''
import importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
before = dict(os.environ)
def audit(event, args):
    if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wa+"))
                            or (isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR))):
        raise AssertionError("write during config import/render")
sys.addaudithook(audit)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread")), \
     patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
     patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
    spec = importlib.util.spec_from_file_location("config_pure", sys.argv[1])
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    lines, values = ["KEEP=yes"], {"NEW": "hello world"}
    result = config.render_env_lines(lines, values, config.format_env_value)
    assert 'NEW="hello world"' in result
    assert lines == ["KEEP=yes"] and values == {"NEW": "hello world"}
assert dict(os.environ) == before
assert not any(name == "phone_worker" or name == "music_agent" for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(PHONE.parent / "phone_worker_runtime/config.py")],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
