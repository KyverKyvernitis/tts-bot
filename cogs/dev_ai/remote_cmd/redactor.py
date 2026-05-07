from __future__ import annotations

import re

SENSITIVE_KEYWORDS = (
    "TOKEN",
    "BOT_TOKEN",
    "DISCORD_TOKEN",
    "API_KEY",
    "APIKEY",
    "SECRET",
    "PASSWORD",
    "PASS",
    "MONGO_URI",
    "MONGODB_URI",
    "DATABASE_URL",
    "WEBHOOK_URL",
    "AUTHORIZATION",
)

# Linhas tipo KEY=value, export KEY=value e saídas de env/printenv.
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)^(?P<prefix>\s*(?:export\s+)?[A-Z0-9_]*(?:TOKEN|API[_-]?KEY|APIKEY|SECRET|PASSWORD|PASS|MONGO(?:DB)?[_-]?URI|DATABASE[_-]?URL|WEBHOOK[_-]?URL|AUTHORIZATION)[A-Z0-9_]*\s*=\s*)(?P<value>.*)$"
)

DISCORD_WEBHOOK_RE = re.compile(
    r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-\.]+",
    re.IGNORECASE,
)

AUTH_HEADER_RE = re.compile(
    r"(?i)\b(Authorization\s*[:=]\s*(?:Bearer|Bot)?\s*)[A-Za-z0-9_\-.=:/+]{12,}"
)

MONGO_URL_PASSWORD_RE = re.compile(
    r"(?i)\b(mongodb(?:\+srv)?://[^:\s/@]+:)([^@\s/]+)(@[^\s]+)"
)

GENERIC_URL_PASSWORD_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^:\s/@]+:)([^@\s/]+)(@)"
)

DISCORD_TOKEN_RE = re.compile(
    r"\b(?:mfa\.[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,10}\.[A-Za-z0-9_\-]{20,})\b"
)

COMMON_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9_\-]{20,})\b",
    re.IGNORECASE,
)


def redact_text(text: object) -> str:
    """Remove segredos de qualquer texto antes de enviar pelo Discord.

    O `_cmd` pode ser usado fora do canal privado da DevAI, então nenhuma
    resposta pública deve carregar tokens, webhooks, URIs com senha ou linhas
    de `.env` com valor sensível. O comando continua executando normalmente;
    só a visualização é redigida.
    """
    if text is None:
        return ""
    value = str(text)
    if not value:
        return ""

    def _assignment_repl(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}[REDACTED]"

    value = SENSITIVE_ASSIGNMENT_RE.sub(_assignment_repl, value)
    value = DISCORD_WEBHOOK_RE.sub("[REDACTED_DISCORD_WEBHOOK]", value)
    value = AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", value)
    value = MONGO_URL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", value)
    value = GENERIC_URL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", value)
    value = DISCORD_TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    value = COMMON_SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", value)
    return value


def redact_bytes(payload: bytes | bytearray | memoryview | None) -> bytes:
    if not payload:
        return b""
    text = bytes(payload).decode("utf-8", errors="replace")
    return redact_text(text).encode("utf-8", errors="replace")
