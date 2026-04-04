#!/usr/bin/env bash
set -u

ENV_FILE="/home/ubuntu/bot/.env"
HOSTNAME="$(hostname)"
NOW="$(date '+%Y-%m-%d %H:%M:%S')"
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
  exit 0
fi

if command -v python3 >/dev/null 2>&1; then
  PAYLOAD="$(TYPE="$TYPE" TITLE="$TITLE" BODY="$BODY" HOSTNAME="$HOSTNAME" NOW="$NOW" NOW_ISO="$NOW_ISO" python3 - <<'PY'
import json
import os
import re

TYPE = os.environ.get("TYPE", "info")
TITLE = os.environ.get("TITLE", "Sem título")
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
    "etapa": "Etapa",
    "branch": "Branch",
    "commit": "Commit",
    "de": "De",
    "para": "Para",
    "host": "Host",
    "hora": "Hora",
    "motivo": "Motivo",
    "arquivos alterados": "Arquivos alterados",
    "healthcheck": "Healthcheck",
    "url": "URL",
    "serviço": "Serviço",
    "servico": "Serviço",
    "tentativa de update para": "Tentativa",
    "rollback para": "Rollback",
    "novo commit remoto detectado": "Novo commit",
    "commit remoto ruim anterior": "Commit ruim anterior",
    "url de verificação": "URL de verificação",
}


def trunc(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def infer_summary(event_type: str, title: str, body: str) -> str:
    hay = f"{title}\n{body}".lower()

    success_checks = [
        (("healthcheck: ok" in hay) and ("activity:" in hay or "backend validado" in hay),
         "Deploy concluído, frontend publicado e backend validado no healthcheck final."),
        (("reiniciado com sucesso" in hay) and ("healthcheck: ok" in hay),
         "Serviço reiniciado e validado com sucesso após o update."),
        ("update automático aplicado" in hay,
         "Update aplicado com sucesso e pipeline finalizado sem rollback."),
        ("update liberado novamente" in hay,
         "O commit remoto voltou a ser elegível e novas tentativas de update foram liberadas."),
        ("novo commit detectado após rollback" in hay,
         "Um commit remoto mais novo foi detectado após o rollback e o auto update foi liberado novamente."),
    ]

    info_checks = [
        ("deploy reiniciado para commit mais novo" in hay,
         "Um commit mais novo chegou durante o deploy e o processo foi reiniciado com segurança."),
        ("deploy reiniciado" in hay,
         "O deploy atual foi interrompido para tentar novamente em um estado mais seguro."),
        ("update ignorado" in hay and "repo sujo" in hay,
         "O update foi ignorado porque o repositório local tinha alterações pendentes."),
    ]

    error_checks = [
        ("requirements.txt", "A etapa de dependências Python falhou fora do diretório esperado."),
        ("pip install -r requirements.txt", "A instalação das dependências Python falhou."),
        ("activity frontend build", "O build do frontend da Activity falhou."),
        ("ts2448", "O TypeScript encontrou uso de variável antes da declaração."),
        ("ts2454", "O TypeScript encontrou variável usada antes de receber valor."),
        ("ts2345", "O TypeScript recebeu um tipo incompatível em uma chamada."),
        ("ts18047", "O TypeScript detectou acesso possível a valor nulo."),
        ("vite build", "O build do frontend falhou durante a etapa do Vite."),
        ("connect() failed", "O serviço de destino não estava respondendo na porta esperada."),
        ("connection refused", "A conexão com o serviço local foi recusada."),
        ("sanity check", "O serviço reiniciou, mas não passou no healthcheck."),
        ("repo sujo", "O auto update foi pausado para não sobrescrever alterações locais."),
        ("rollback", "O sistema voltou para a versão anterior para manter o serviço estável."),
        ("npm install", "A instalação de dependências Node falhou."),
        ("systemctl restart", "A reinicialização do serviço falhou."),
        (("127.0.0.1:8787" in hay) and ("healthcheck: ok" not in hay) and ("backend validado" not in hay),
         "O backend da Activity não estava acessível em 127.0.0.1:8787."),
    ]

    if event_type in {"success", "update"}:
        for matched, summary in success_checks:
            if matched:
                return summary
        return ""

    if event_type == "info":
        for matched, summary in info_checks:
            if matched:
                return summary
        return ""

    for matched, summary in error_checks:
        ok = matched if isinstance(matched, bool) else matched in hay
        if ok:
            return summary
    return ""

lines = BODY.splitlines()
fields = []
extra_notes = []
error_lines = []
collect_error = False
used_labels = set()

for raw_line in lines:
    line = raw_line.rstrip()
    stripped = line.strip()
    if collect_error:
        if stripped == "" and error_lines:
            continue
        error_lines.append(line)
        continue

    if not stripped:
        continue

    if stripped.lower().startswith("últimas linhas do erro"):
        collect_error = True
        continue

    m = re.match(r"^([^:]{1,48}):\s*(.+)$", stripped)
    if m:
        raw_label = m.group(1).strip()
        value = m.group(2).strip()
        label_key = raw_label.lower()
        label = LABEL_MAP.get(label_key, raw_label[:48])
        if label_key not in used_labels:
            used_labels.add(label_key)
            fields.append({
                "name": trunc(label, 256),
                "value": trunc(value or "—", 1024),
                "inline": label not in {"Motivo", "Etapa", "URL de verificação"},
            })
        else:
            extra_notes.append(stripped)
        continue

    extra_notes.append(stripped)

summary = infer_summary(TYPE, TITLE, BODY)

if summary:
    fields.insert(0, {"name": "Leitura", "value": trunc(summary, 1024), "inline": False})

fields.insert(0, {"name": "Hora", "value": NOW or "—", "inline": True})
fields.insert(0, {"name": "Host", "value": HOSTNAME or "—", "inline": True})

if error_lines:
    cleaned_error = "\n".join([line for line in error_lines if line.strip()])
    fields.append({
        "name": "Últimas linhas do erro",
        "value": trunc(f"```\n{cleaned_error}\n```", 1024),
        "inline": False,
    })

# description tries to be short and useful
paragraphs = []
if extra_notes:
    paragraphs.append(trunc("\n".join(extra_notes), 900))
if not paragraphs and BODY.strip():
    body_clean = compact(BODY)
    if body_clean:
        paragraphs.append(trunc(body_clean, 900))
if not paragraphs:
    paragraphs.append("Atualização registrada pelo pipeline de monitoramento.")

description = "\n\n".join(paragraphs)

payload = {
    "username": "bot status",
    "embeds": [
        {
            "title": f"{EMOJI_MAP.get(TYPE, 'ℹ️')} {TITLE}",
            "description": description,
            "color": COLOR_MAP.get(TYPE, COLOR_MAP["info"]),
            "fields": fields[:10],
            "footer": {"text": f"Monitoramento • {HOSTNAME}"},
            "timestamp": NOW_ISO or None,
        }
    ],
}

print(json.dumps(payload, ensure_ascii=False))
PY
)"

  if [ -n "$PAYLOAD" ]; then
    curl -fsS -H "Content-Type: application/json" \
      -X POST \
      -d "$PAYLOAD" \
      "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || true
    exit 0
  fi
fi

escape_json() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; :a;N;$!ba;s/\n/\\n/g'
}

case "$TYPE" in
  error)   EMOJI="❌" ;;
  warn)    EMOJI="⚠️" ;;
  success) EMOJI="✅" ;;
  update)  EMOJI="🔄" ;;
  *)       EMOJI="ℹ️" ;;
esac

MESSAGE="$EMOJI $TITLE
Host: $HOSTNAME
Hora: $NOW

$BODY"

ESCAPED_MESSAGE="$(escape_json "$MESSAGE")"

curl -fsS -H "Content-Type: application/json" \
  -X POST \
  -d "{\"content\":\"$ESCAPED_MESSAGE\"}" \
  "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || true
