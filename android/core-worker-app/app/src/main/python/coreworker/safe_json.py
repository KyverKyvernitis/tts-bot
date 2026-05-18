import json
import os
import re
import time
from pathlib import Path

_SECRET_RE = re.compile(r"(?i)(token|authorization|bearer|secret|password|passwd|firebase|fcm)[=: ]+[^\\s]+")
_IPV4_RE = re.compile(r"(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")


def now_ms() -> int:
    return int(time.time() * 1000)


def load_context(context_json):
    if isinstance(context_json, dict):
        return context_json
    if context_json is None:
        return {}
    try:
        return json.loads(str(context_json))
    except Exception as exc:
        return {"_context_error": f"{type(exc).__name__}: {exc}"}


def clean_text(value, limit=1200):
    text = str(value if value is not None else "")
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _IPV4_RE.sub("[ip-redacted]", text)
    if len(text) > limit:
        text = text[:limit] + "…[truncated]"
    return text


def safe_path(path_value):
    text = str(path_value or "")
    if not text:
        return ""
    # Só expõe o final do caminho para evitar vazar detalhes desnecessários.
    try:
        p = Path(text)
        parts = p.parts[-3:]
        return "/".join(parts)
    except Exception:
        return clean_text(text, 160)


def dir_size(path_value, max_files=400):
    path = Path(str(path_value or ""))
    total = 0
    files = 0
    errors = 0
    if not path.exists():
        return {"exists": False, "bytes": 0, "files": 0, "errors": 0}
    try:
        if path.is_file():
            return {"exists": True, "bytes": path.stat().st_size, "files": 1, "errors": 0}
        for root, dirs, filenames in os.walk(path):
            for name in filenames:
                if files >= max_files:
                    return {"exists": True, "bytes": total, "files": files, "errors": errors, "truncated": True}
                try:
                    fp = Path(root) / name
                    total += fp.stat().st_size
                    files += 1
                except Exception:
                    errors += 1
    except Exception:
        errors += 1
    return {"exists": True, "bytes": total, "files": files, "errors": errors}


def ok_response(kind, summary, **extra):
    payload = {
        "ok": True,
        "kind": kind,
        "summary": clean_text(summary, 180),
        "generatedAtMs": now_ms(),
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def error_response(kind, exc):
    return json.dumps({
        "ok": False,
        "kind": kind,
        "summary": "Python interno falhou",
        "error": clean_text(f"{type(exc).__name__}: {exc}", 300),
        "generatedAtMs": now_ms(),
    }, ensure_ascii=False, sort_keys=True)
