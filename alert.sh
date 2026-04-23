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
from collections import OrderedDict

TYPE = os.environ.get("TYPE", "info").strip().lower()
TITLE = os.environ.get("TITLE", "Sem título").strip()
BODY = os.environ.get("BODY", "")
HOSTNAME = os.environ.get("HOSTNAME", "unknown")
NOW = os.environ.get("NOW", "")
NOW_ISO = os.environ.get("NOW_ISO", "")

COMPONENTS_V2_FLAG = 1 << 15
TEXT_DISPLAY = 10
SEPARATOR = 14
CONTAINER = 17

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
    "bot": "Bot health",
    "bot health": "Bot health",
    "bot healthcheck": "Bot health",
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

HEADER_FIELDS = {
    "Host",
    "Branch",
    "Commit",
    "Mudança",
    "Rollback",
    "Duração",
    "Serviço",
    "Resultado",
    "Código",
    "Status",
    "Estado ativo",
    "Subestado",
}
STATUS_FIELDS = {"Bot health", "Frontend", "Backend", "Activity"}
DETAIL_FIELDS = {"Etapa", "Motivo", "URL"}
BULLET_FIELDS = {"Arquivos"}
CODE_FIELDS = {"Últimas linhas", "Comando"}
MAX_TEXT = 1800
MAX_TOTAL_TEXT = 3800
MAX_CODE = 1200


def trunc(value: str, limit: int) -> str:
    value = (value or "").strip()
    if not value:
        return "—"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def normalize_lines(body: str):
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def strip_outer_code_fences(value: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while True:
        match = re.match(r"^```[A-Za-z0-9_-]*\n?(.*?)\n?```$", text, flags=re.DOTALL)
        if not match:
            return text
        inner = match.group(1).strip("\n").strip()
        if inner == text:
            return text
        text = inner


def strip_lonely_fence_lines(value: str) -> str:
    cleaned = []
    for raw in (value or "").splitlines():
        stripped = raw.strip()
        if stripped in {"`", "``", "```"}:
            continue
        if re.fullmatch(r"```[A-Za-z0-9_-]+", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()


def clean_field_text(value: str) -> str:
    text = (value or "").replace("\t", "  ")
    text = strip_outer_code_fences(text)
    text = strip_lonely_fence_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_multiline_bullets(value: str) -> str:
    raw = clean_field_text(value)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return "—"
    normalized = []
    for line in lines:
        line = re.sub(r"^[•\-]\s*", "", line).strip()
        if line.startswith("`") and line.endswith("`") and len(line) >= 2:
            line = line[1:-1].strip()
        line = line.strip("`").strip()
        if not line or line in {"—", "`", "``", "```"}:
            continue
        normalized.append(f"- `{line}`")
    return trunc("\n".join(normalized) if normalized else "—", 1500)


def format_code_block(value: str) -> str:
    raw = clean_field_text(value) or "—"
    raw = raw.replace("```", "ʼʼʼ")
    raw = trunc(raw, MAX_CODE)
    return f"```text\n{raw}\n```"


def format_field_value(name: str, value: str) -> str:
    value = clean_field_text(value) or "—"
    if name in BULLET_FIELDS:
        return format_multiline_bullets(value)
    if name in CODE_FIELDS:
        return format_code_block(value)
    if "\n" in value and len([line for line in value.splitlines() if line.strip()]) >= 3:
        return format_code_block(value)
    return trunc(value, 1200)


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

        match = re.match(r"^([^:]{1,48}):\s*(.*)$", stripped)
        if match:
            raw_label = match.group(1).strip()
            value = match.group(2).rstrip()
            key = raw_label.lower()
            label = LABEL_MAP.get(key, raw_label[:48])

            if key == "resumo":
                description = trunc(value, 1200)
                current_idx = None
                continue

            if key == "hora":
                footer = value or footer
                current_idx = None
                continue

            if key == "host" and not value:
                value = HOSTNAME

            initial_value = format_field_value(label, value) if value else ""
            fields.append({"name": trunc(label, 80), "value": initial_value})
            current_idx = len(fields) - 1
            continue

        if current_idx is not None:
            prev = fields[current_idx]["value"]
            if prev.startswith("```text\n") and prev.endswith("\n```"):
                prev_plain = prev[len("```text\n"):-len("\n```")]
            else:
                prev_plain = "" if prev == "—" else prev
            joined = stripped if not prev_plain else f"{prev_plain}\n{stripped}"
            fields[current_idx]["value"] = format_field_value(fields[current_idx]["name"], joined)
        elif description:
            description = trunc(f"{description}\n{stripped}", 1200)
        else:
            description = trunc(stripped, 1200)

    cleaned = []
    for field in fields:
        value = field["value"].strip()
        if field["name"].strip().lower() == "arquivos" and value in {"", "—"}:
            continue
        if not value:
            field["value"] = "—"
        cleaned.append(field)
    return description or "Notificação automática.", cleaned[:25], footer or NOW


def split_text(text: str, limit: int = MAX_TEXT):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 3:
            split_at = limit
        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return pieces


def make_text(content: str):
    return {"type": TEXT_DISPLAY, "content": content}


def make_separator():
    return {"type": SEPARATOR, "divider": True, "spacing": 1}


def render_field_block(title: str, pairs):
    if not pairs:
        return []
    parts = [f"## {title}"]
    for name, value in pairs:
        value = (value or "—").strip() or "—"
        if value.startswith("```") or "\n" in value:
            parts.append(f"### {name}\n{value}")
        else:
            parts.append(f"- **{name}:** {value}")
    joined = "\n\n".join(parts)
    return [make_text(chunk) for chunk in split_text(joined)]


def render_code_block(name: str, value: str):
    raw = value.strip()
    block = raw if raw.startswith("```") else format_code_block(raw)
    return [make_text(chunk) for chunk in split_text(f"## {name}\n{block}")]


def append_container(components, color, children):
    normalized = []
    for child in children:
        if not child:
            continue
        if child["type"] == TEXT_DISPLAY:
            content = child.get("content", "").strip()
            if not content:
                continue
            normalized.append({"type": TEXT_DISPLAY, "content": content})
        else:
            normalized.append(child)
    if normalized:
        components.append({"type": CONTAINER, "accent_color": color, "components": normalized[:10]})


def build_containers(title: str, summary: str, fields, footer: str, color: int):
    field_map = OrderedDict((field["name"], field["value"]) for field in fields)

    header_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in HEADER_FIELDS]
    status_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in STATUS_FIELDS]
    detail_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in DETAIL_FIELDS]
    file_value = field_map.pop("Arquivos", "")
    command_value = field_map.pop("Comando", "")
    logs_value = field_map.pop("Últimas linhas", "")
    other_pairs = list(field_map.items())

    header_meta = []
    host = next((value for name, value in header_pairs if name == "Host"), None)
    branch = next((value for name, value in header_pairs if name == "Branch"), None)
    commit = next((value for name, value in header_pairs if name == "Commit"), None)
    if host and host != "—":
        header_meta.append(f"host `{host}`")
    if branch and branch != "—":
        header_meta.append(f"branch `{branch}`")
    if commit and commit != "—":
        header_meta.append(f"commit `{commit}`")
    header_meta.append(footer)

    components = []
    primary_children = [make_text(f"# {title}"), make_text("-# " + " • ".join(part for part in header_meta if part))]
    if summary and summary != "—":
        primary_children.append(make_separator())
        primary_children.extend(make_text(chunk) for chunk in split_text(summary, 1000))
    if header_pairs:
        primary_children.append(make_separator())
        primary_children.extend(render_field_block("Resumo rápido", header_pairs))
    if status_pairs:
        primary_children.append(make_separator())
        primary_children.extend(render_field_block("Status dos serviços", status_pairs))
    append_container(components, color, primary_children)

    secondary_children = []
    if detail_pairs:
        secondary_children.extend(render_field_block("Detalhes", detail_pairs))
    if other_pairs:
        if secondary_children:
            secondary_children.append(make_separator())
        secondary_children.extend(render_field_block("Informações extras", other_pairs))
    if file_value and file_value != "—":
        if secondary_children:
            secondary_children.append(make_separator())
        secondary_children.extend(make_text(chunk) for chunk in split_text(f"## Arquivos alterados\n{file_value}"))
    append_container(components, color, secondary_children)

    log_children = []
    if command_value and command_value != "—":
        log_children.extend(render_code_block("Comando", command_value))
    if logs_value and logs_value != "—":
        if log_children:
            log_children.append(make_separator())
        log_children.extend(render_code_block("Últimas linhas", logs_value))
    append_container(components, color, log_children)

    total_chars = 0
    for container in components:
        for child in container["components"]:
            if child["type"] != TEXT_DISPLAY:
                continue
            remaining = max(400, MAX_TOTAL_TEXT - total_chars)
            child["content"] = trunc(child["content"], remaining)
            total_chars += len(child["content"])

    return components[:4]


description, fields, footer = parse_body(BODY)
emoji = EMOJI_MAP.get(TYPE, "ℹ️")
color = COLOR_MAP.get(TYPE, COLOR_MAP["info"])
full_title = TITLE if TITLE.startswith(("❌", "⚠️", "✅", "🔄", "ℹ️")) else f"{emoji} {TITLE}"

payload = {
    "allowed_mentions": {"parse": []},
    "flags": COMPONENTS_V2_FLAG,
    "components": build_containers(trunc(full_title, 120), description, fields, trunc(footer, 200), color),
}

print(json.dumps(payload, ensure_ascii=False))
PY
})" || exit 1

WEBHOOK_URL="$ALERT_WEBHOOK_URL"
case "$WEBHOOK_URL" in
  *with_components=*) ;;
  *\?*) WEBHOOK_URL="${WEBHOOK_URL}&with_components=true" ;;
  *) WEBHOOK_URL="${WEBHOOK_URL}?with_components=true" ;;
esac

TMP_RESP="$(mktemp)"
HTTP_CODE="$({
  curl -sS \
    -o "$TMP_RESP" \
    -w '%{http_code}' \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD_JSON" \
    "$WEBHOOK_URL"
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
