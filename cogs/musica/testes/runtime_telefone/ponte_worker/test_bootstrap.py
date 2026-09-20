from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[5]
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("music_bootstrap_boundary_test", PHONE)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_startup_loads_configuration_and_persists_music_token(tmp_path, monkeypatch):
    config = tmp_path / "phone.env"
    config.write_text("PHONE_WORKER_PORT=8791\nMUSIC_AGENT_AUTO_TOKEN=1\n", encoding="utf-8")
    music = tmp_path / "music.env"
    monkeypatch.setenv("PHONE_WORKER_ENV", str(config))
    monkeypatch.setenv("MUSIC_AGENT_ENV", str(music))
    monkeypatch.delenv("MUSIC_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("PHONE_WORKER_PORT", raising=False)
    monkeypatch.setenv("MUSIC_AGENT_AUTO_TOKEN", "1")
    worker = _load_worker()
    calls = []
    monkeypatch.setattr(worker, "_load_persisted_pending_core_job_results", lambda: None)
    monkeypatch.setattr(worker, "_send_core_worker_heartbeat_once", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr(sys, "argv", [str(PHONE), "--heartbeat-once"])
    assert worker.main() == 0
    assert calls[0]["port"] == 8791
    assert os.environ["MUSIC_AGENT_TOKEN"] in music.read_text(encoding="utf-8")
    assert music.stat().st_mode & 0o777 == 0o600
