import os
import time
from typing import Any, Callable, Dict

from flask import Flask, jsonify

app = Flask(__name__)

_started_at = time.time()
_health_provider: Callable[[], Dict[str, Any]] | None = None


def set_health_provider(provider: Callable[[], Dict[str, Any]]):
    global _health_provider
    _health_provider = provider


def _default_health() -> Dict[str, Any]:
    return {
        "status": "starting",
        "healthy": True,
        "starting": True,
        "uptime_seconds": round(time.time() - _started_at, 2),
    }


def _resolve_health() -> Dict[str, Any]:
    data = _default_health()
    if _health_provider is not None:
        try:
            provided = _health_provider() or {}
            if isinstance(provided, dict):
                data.update(provided)
        except Exception as e:
            data.update({
                "status": "error",
                "healthy": False,
                "starting": False,
                "error": f"health provider failed: {e}",
            })

    data.setdefault("uptime_seconds", round(time.time() - _started_at, 2))
    data.setdefault("healthy", True)
    data.setdefault("starting", False)
    data.setdefault("status", "ok" if data.get("healthy") else "error")
    return data


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    data = _resolve_health()
    code = 200 if data.get("healthy") or data.get("starting") else 503
    return jsonify(data), code


@app.get("/healthz")
def healthz():
    data = _resolve_health()
    code = 200 if data.get("healthy") or data.get("starting") else 503
    return jsonify(data), code


def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print("WEB SERVER INICIANDO")
    print(f"[webserver] usando porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
