#!/usr/bin/env bash
set -euo pipefail

# O antigo webhook foi aposentado. Este script mantém a interface usada por
# systemd/updater, mas agora apenas persiste um evento para o próprio bot enviar
# ao canal técnico de logs. Assim nenhum processo shell possui responsabilidade
# de falar com a API do Discord e os eventos sobrevivem a restart/offline do bot.

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
TYPE="${1:-info}"
TITLE="${2:-Sem título}"
BODY="${3:-}"
ATTACH_FILE="${4:-}"
ATTACH_NAME="${5:-}"
EVENT_ID="${6:-}"
OUTBOX_DIR="${UPDATE_LOG_OUTBOX_DIR:-${UPDATE_ALERT_OUTBOX_DIR:-$REPO_DIR/data/runtime/update-alert-outbox}}"

command -v python3 >/dev/null 2>&1 || exit 1

TYPE="$TYPE" TITLE="$TITLE" BODY="$BODY" ATTACH_FILE="$ATTACH_FILE" \
ATTACH_NAME="$ATTACH_NAME" EVENT_ID="$EVENT_ID" OUTBOX_DIR="$OUTBOX_DIR" \
ALERT_DRY_RUN="${ALERT_DRY_RUN:-0}" python3 - <<'PY'
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import shutil
import time
import uuid

root = pathlib.Path(os.environ["OUTBOX_DIR"])
alert_type = (os.environ.get("TYPE") or "info").strip().lower()
title = (os.environ.get("TITLE") or "Sem título").strip()
body = os.environ.get("BODY") or ""
attachment_name = pathlib.Path(os.environ.get("ATTACH_NAME") or "").name
requested_event_id = (os.environ.get("EVENT_ID") or "").strip()

if requested_event_id:
    event_id = requested_event_id
else:
    seed = f"{title}\0{body}".encode("utf-8", errors="ignore")
    event_id = f"log-{int(time.time() * 1000)}-{hashlib.sha256(seed).hexdigest()[:12]}-{uuid.uuid4().hex[:6]}"

safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in event_id)[:120] or uuid.uuid4().hex
payload = {
    "schema_version": 2,
    "event_id": event_id,
    "type": alert_type,
    "title": title,
    "body": body,
    "attachment": "",
    "attachment_name": attachment_name,
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "attempts": 0,
    "last_error": "",
    "delivery": "discord_bot",
}

source_raw = (os.environ.get("ATTACH_FILE") or "").strip()
source = pathlib.Path(source_raw) if source_raw else None

if os.environ.get("ALERT_DRY_RUN", "0") == "1":
    if source is not None and source.is_file():
        payload["attachment"] = str(source)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0)

root.mkdir(parents=True, exist_ok=True)
job_path = root / f"{safe_id}.json"
if job_path.exists():
    # Mesmo event_id é idempotente. O bot grava recibo somente após o Discord
    # confirmar o envio; enquanto isso basta manter um único job na fila.
    raise SystemExit(0)

if source is not None and source.is_file() and source.stat().st_size > 0:
    suffix = source.suffix[:16]
    target = root / f"{safe_id}{suffix}.attachment"
    shutil.copy2(source, target)
    payload["attachment"] = str(target)

# Gravação atômica: o consumidor nunca enxerga JSON parcial.
tmp = root / f".{safe_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.chmod(tmp, 0o664)
os.replace(tmp, job_path)
try:
    os.chmod(job_path, 0o664)
except OSError:
    pass
PY
