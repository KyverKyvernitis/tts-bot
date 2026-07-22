from __future__ import annotations

import json
import os
import time
from pathlib import Path

from utility.update_delivery import enqueue_job, flush_jobs, migrate_legacy_jobs, prune_jobs, recover_sending


def test_enqueue_is_idempotent_after_delivery(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    assert enqueue_job(root, kind="status", event_key="UPD-1:final", data={"value": 1}) == "queued"
    sent: list[dict] = []
    result = flush_jobs(root, kind="status", sender=sent.append)
    assert result.delivered == 1
    assert sent == [{"value": 1}]
    assert enqueue_job(root, kind="status", event_key="UPD-1:final", data={"value": 2}) == "delivered"
    result = flush_jobs(root, kind="status", sender=sent.append)
    assert result.delivered == 0
    assert sent == [{"value": 1}]


def test_failed_delivery_returns_to_pending_and_then_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    enqueue_job(root, kind="status", event_key="UPD-2:final", data={"ok": True})
    attempts = 0

    def sender(payload: dict) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("bot reiniciando")
        assert payload["ok"] is True

    first = flush_jobs(root, kind="status", sender=sender, max_attempts=5)
    assert first.retried == 1
    assert list((root / "pending").glob("*.json"))
    second = flush_jobs(root, kind="status", sender=sender, max_attempts=5)
    assert second.delivered == 1
    assert not list((root / "pending").glob("*.json"))


def test_legacy_jobs_are_quarantined_and_not_replayed(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    root.mkdir()
    (root / "old.json").write_text(json.dumps({"payload": {"old": True}}), encoding="utf-8")
    assert migrate_legacy_jobs(root) == 1
    assert not (root / "old.json").exists()
    assert list((root / "legacy").glob("*.json"))
    sent: list[dict] = []
    result = flush_jobs(root, kind="status", sender=sent.append)
    assert sent == []
    assert result.delivered == 0


def test_stale_sending_job_is_recovered(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    enqueue_job(root, kind="status", event_key="UPD-3:final", data={"x": 1})
    pending = next((root / "pending").glob("*.json"))
    sending = root / "sending" / pending.name
    os.replace(pending, sending)
    data = json.loads(sending.read_text(encoding="utf-8"))
    data["claimed_at"] = time.time() - 300
    sending.write_text(json.dumps(data), encoding="utf-8")
    assert recover_sending(root, stale_seconds=30) == 1
    assert (root / "pending" / sending.name).exists()


def test_alert_attachment_is_copied_into_outbox(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    source = tmp_path / "updater.log"
    source.write_text("erro detalhado", encoding="utf-8")
    enqueue_job(
        root,
        kind="alert",
        event_key="UPD-4:error",
        data={"type": "error", "title": "Falha", "body": "x", "attachment": str(source)},
    )
    source.unlink()
    pending = next((root / "pending").glob("*.json"))
    job = json.loads(pending.read_text(encoding="utf-8"))
    stored = Path(job["data"]["attachment"])
    assert stored.is_file()
    assert stored.read_text(encoding="utf-8") == "erro detalhado"


def test_prune_never_deletes_pending_jobs(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    enqueue_job(root, kind="status", event_key="UPD-5:final", data={"x": 1})
    pending = next((root / "pending").glob("*.json"))
    old = time.time() - 60 * 86400
    os.utime(pending, (old, old))
    prune_jobs(root, delivered_days=1, dead_days=1, legacy_days=1)
    assert pending.exists()


def test_legacy_sending_suffix_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    root.mkdir()
    old = root / "old.json.sending"
    old.write_text(json.dumps({"payload": {"old": True}}), encoding="utf-8")
    assert migrate_legacy_jobs(root) == 1
    assert not old.exists()
    assert list((root / "legacy").iterdir())


def test_recent_legacy_final_status_is_migrated_once(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    root.mkdir()
    legacy = root / "recent.json"
    legacy.write_text(
        json.dumps(
            {
                "created_at": time.time(),
                "payload": {
                    "channel_id": "1",
                    "message_id": "2",
                    "candidate_id": "zip-current",
                    "status": "success",
                    "title": "Atualização concluída",
                },
            }
        ),
        encoding="utf-8",
    )
    sent: list[dict] = []
    result = flush_jobs(root, kind="status", sender=sent.append)
    assert result.delivered == 1
    assert sent[0]["candidate_id"] == "zip-current"
    assert sent[0]["terminal"] is True
    assert list((root / "legacy").glob("*.json"))


def test_old_legacy_final_status_is_not_replayed(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    root.mkdir()
    legacy = root / "old-final.json"
    legacy.write_text(
        json.dumps(
            {
                "created_at": time.time() - 86400,
                "payload": {
                    "channel_id": "1",
                    "message_id": "2",
                    "candidate_id": "zip-old",
                    "status": "error",
                },
            }
        ),
        encoding="utf-8",
    )
    sent: list[dict] = []
    result = flush_jobs(root, kind="status", sender=sent.append)
    assert result.delivered == 0
    assert sent == []
    assert list((root / "legacy").glob("*.json"))
