#!/usr/bin/env bash
set -u

ENV_FILE="/home/ubuntu/bot/.env"
HOSTNAME="$(hostname)"
NOW="$(date '+%d/%m/%Y %H:%M:%S')"
NOW_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

TYPE="${1:-info}"
TITLE="${2:-Sem título}"
BODY="${3:-}"

if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  exit 1
fi

PAYLOAD_JSON="$({
TYPE="$TYPE" \
TITLE="$TITLE" \
BODY="$BODY" \
HOSTNAME="$HOSTNAME" \
NOW="$NOW" \
NOW_ISO="$NOW_ISO" \
python3 - <<'PY'
import json
import os
import re

TYPE = os.environ.get("TYPE", "info").strip().lower()
TITLE = os.environ.get("TITLE", "Sem título").strip()
BODY = os.environ.get("BODY", "")
HOSTNAME = os.environ.get("HOSTNAME", "unknown")
NOW = os.environ.get("NOW", "")
NOW_ISO = os.environ.get("NOW_ISO", "")

COLOR_MAP = {
    "error": 0xED4245,
    "warn": 0xF5A524,
    "success": 0x57F287,
    "update": 0x5865F2,
    "info": 0x3BA55D,
}

EMOJI_MAP = {
    "error": "❌",
    "warn": "⚠️",
    "success": "✅",
    "update": "🔄",
    "info": "ℹ️",
}

LABEL_MAP = {
    "resumo": "Resumo",
    "host": "Host",
    "branch": "Branch",
    "commit": "Commit",
    "mudança": "Mudança",
    "mudanca": "Mudança",
    "arquivos": "Arquivos",
    "arquivos alterados": "Arquivos",
    "bot": "Bot",
    "bot health": "Bot",
    "bot healthcheck": "Bot",
    "frontend": "Frontend",
    "backend": "Backend",
    "activity": "Activity",
    "rollback": "Rollback",
    "duração": "Duração",
    "duracao": "Duração",
    "hora": "Hora",
    "motivo": "Motivo",
    "url": "URL",
    "etapa": "Etapa",
    "comando": "Comando",
    "serviço": "Serviço",
    "servico": "Serviço",
    "activestate": "Estado ativo",
    "substate": "Subestado",
    "result": "Resultado",
    "execmaincode": "Código",
    "execmainstatus": "Status",
    "últimas linhas": "Últimas linhas",
    "ultimas linhas": "Últimas linhas",
    "últimas linhas do erro": "Últimas linhas",
    "ultimas linhas do erro": "Últimas linhas",
}

INLINE_FIELDS = {"Host", "Branch", "Commit", "Duração", "Serviço", "Bot", "Activity", "Resultado", "Status", "Código"}
BULLET_FIELDS = {"Arquivos"}
CODE_FIELDS = {"Últimas linhas", "Comando"}


def trunc(value: str, limit: int) -> str:
    value = (value or "").strip()
    if not value:
        return "—"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def normalize_lines(body: str):
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def format_multiline_bullets(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "—"
    normalized = []
    for line in lines:
        if line.startswith("• "):
            normalized.append(line)
        elif line.startswith("- "):
            normalized.append(f"• {line[2:].strip()}")
        else:
            normalized.append(f"• {line}")
    return trunc("\n".join(normalized), 1024)


def format_code_block(value: str) -> str:
    raw = (value or "").strip() or "—"
    limit = 1012
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    return f"```text\n{raw}\n```"


def format_field_value(name: str, value: str) -> str:
    value = (value or "").replace("\t", "  ").strip() or "—"
    if name in BULLET_FIELDS:
        return format_multiline_bullets(value)
    if name in CODE_FIELDS:
        return format_code_block(value)
    if "\n" in value and len(value.splitlines()) >= 3:
        return format_code_block(value)
    return trunc(value, 1024)


def parse_body(body: str):
    lines = normalize_lines(body)
    fields = []
    description = ""
    footer = NOW
    current_idx = None

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue

        m = re.match(r"^([^:]{1,48}):\s*(.*)$", stripped)
        if m:
            raw_label = m.group(1).strip()
            value = m.group(2).rstrip()
            key = raw_label.lower()
            label = LABEL_MAP.get(key, raw_label[:48])

            if key == "resumo":
                description = trunc(value, 4096)
                current_idx = None
                continue

            if key == "hora":
                footer = value or footer
                current_idx = None
                continue

            if key == "host" and not value:
                value = HOSTNAME

            fields.append({
                "name": trunc(label, 256),
                "value": format_field_value(label, value or "—"),
                "inline": label in INLINE_FIELDS,
            })
            current_idx = len(fields) - 1
        else:
            if current_idx is not None:
                prev = fields[current_idx]["value"]
                if prev.startswith("```text\n") and prev.endswith("\n```"):
                    prev_plain = prev[len("```text\n"):-len("\n```")]
                else:
                    prev_plain = "" if prev == "—" else prev
                joined = stripped if not prev_plain else f"{prev_plain}\n{stripped}"
                fields[current_idx]["value"] = format_field_value(fields[current_idx]["name"], joined)
            elif description:
                description = trunc(f"{description}\n{stripped}", 4096)
            else:
                description = trunc(stripped, 4096)

    cleaned = []
    for field in fields:
        norm_name = field["name"].strip().lower()
        norm_value = field["value"].strip()
        if norm_name == "arquivos" and norm_value in {"", "—"}:
            continue
        cleaned.append(field)

    return description, cleaned[:25], footer


description, fields, footer = parse_body(BODY)

if not description or description == "—":
    description = "Notificação automática."

emoji = EMOJI_MAP.get(TYPE, "ℹ️")
color = COLOR_MAP.get(TYPE, COLOR_MAP["info"])
full_title = TITLE if TITLE.startswith(("❌", "⚠️", "✅", "🔄", "ℹ️")) else f"{emoji} {TITLE}"

payload = {
    "allowed_mentions": {"parse": []},
    "embeds": [
        {
            "title": trunc(full_title, 256),
            "description": trunc(description, 4096),
            "color": color,
            "fields": fields,
            "footer": {"text": trunc(footer or NOW, 2048)},
            "timestamp": NOW_ISO,
        }
    ],
}

print(json.dumps(payload, ensure_ascii=False))
PY
})" || exit 1

TMP_RESP="$(mktemp)"
HTTP_CODE="$({
  curl -sS \
    -o "$TMP_RESP" \
    -w '%{http_code}' \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD_JSON" \
    "$ALERT_WEBHOOK_URL"
})" || {
  rm -f "$TMP_RESP"
  exit 1
}

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "204" ]; then
  cat "$TMP_RESP" >&2
  rm -f "$TMP_RESP"
  exit 1
fi

rm -f "$TMP_RESP"
exit 0
