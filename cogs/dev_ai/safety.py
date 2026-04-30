from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Atribuição com VALOR aspeado: `webhook_url = "https://..."`,
    # `api_key: 'gsk_...'`. Exige string literal de 8+ chars, não captura
    # type hints como `webhook_url: str = ""` ou `def f(api_key=DEFAULT)`.
    re.compile(
        r"(?i)\b(discord[_-]?token|bot[_-]?token|client[_-]?secret|api[_-]?key|webhook[_-]?url|authorization|bearer)\b"
        r"\s*[:=]\s*"
        r"(['\"])([^'\"\s]{8,})\2"
    ),
    # Tokens conhecidos por prefixo — sempre redact independente de contexto.
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gsk_[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk_[0-9A-Za-z_\-]{20,}"),
    re.compile(r"hf_[0-9A-Za-z_\-]{20,}"),
    re.compile(r"cfut_[0-9A-Za-z_\-]{20,}"),
    re.compile(r"csk-[0-9A-Za-z_\-]{20,}"),  # Cerebras
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-\.]+"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL),
)

BLOCKED_EXACT_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "google-credentials.json",
    "credentials.json",
    "service-account.json",
}

BLOCKED_PARTS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "tmp_audio",
}

BLOCKED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}

ALLOWED_PATCH_SUFFIXES = {
    ".py",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".service",
    ".timer",
}

ALLOWED_PATCH_EXACT = {
    "requirements.txt",
    "Dockerfile",
}


def redact_secrets(text: str, *, max_chars: int | None = None) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            # Padrão 1 (atribuição): grupos = (key, quote, value)
            #   webhook_url = "abc..." → webhook_url=<redacted>
            # Padrão 2 (token solto): grupos vazios → <redacted-secret>
            if match.lastindex and match.lastindex >= 3:
                return f"{match.group(1)}=<redacted>"
            return "<redacted-secret>"
        value = pattern.sub(repl, value)
    if max_chars is not None and len(value) > max_chars:
        value = value[-max_chars:]
    return value


def normalize_rel_path(raw_path: str) -> Path:
    raw = str(raw_path or "").replace("\\", "/").strip().lstrip("/")
    posix = PurePosixPath(raw)
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"caminho inválido: {raw_path!r}")
    if any(part in BLOCKED_PARTS for part in parts):
        raise ValueError(f"caminho bloqueado: {raw_path}")
    rel = Path(*parts)
    lower_name = rel.name.lower()
    if lower_name in BLOCKED_EXACT_NAMES:
        raise ValueError(f"arquivo sensível bloqueado: {rel.as_posix()}")
    if rel.suffix.lower() in BLOCKED_SUFFIXES:
        raise ValueError(f"extensão sensível bloqueada: {rel.as_posix()}")
    return rel


def is_safe_to_read(rel_path: Path) -> bool:
    try:
        rel = normalize_rel_path(rel_path.as_posix())
    except Exception:
        return False
    if rel.name.lower() in BLOCKED_EXACT_NAMES:
        return False
    if rel.suffix.lower() in BLOCKED_SUFFIXES:
        return False
    return True


def is_safe_patch_path(rel_path: Path) -> bool:
    try:
        rel = normalize_rel_path(rel_path.as_posix())
    except Exception:
        return False
    posix = rel.as_posix()
    if posix in ALLOWED_PATCH_EXACT:
        return True
    if rel.suffix.lower() in ALLOWED_PATCH_SUFFIXES:
        return True
    return False


def safe_join(root: Path, rel_path: Path) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / rel_path).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"arquivo fora do projeto: {rel_path.as_posix()}")
    return target
