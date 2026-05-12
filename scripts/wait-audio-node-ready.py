#!/usr/bin/env python3
"""Espera o node de áudio compatível com Lavalink API ficar pronto.

Usado pelo start.sh/systemd para evitar que o Wavelink tente conectar enquanto
Java/Lavalink ou NodeLink ainda está subindo. Se o node estiver desativado, sai
com sucesso imediatamente.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def _base_url(host: str, port: int, secure: bool) -> str:
    host = (host or "").strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    scheme = "https" if secure else "http"
    if ":" in host.rsplit("/", 1)[-1]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _selected_node() -> dict[str, object] | None:
    provider = (os.getenv("MUSIC_NODE_PROVIDER", "lavalink") or "lavalink").strip().lower()
    if provider in {"node", "node-link"}:
        provider = "nodelink"
    if provider not in {"lavalink", "nodelink", "auto"}:
        provider = "lavalink"

    lavalink_enabled = _as_bool(os.getenv("LAVALINK_ENABLED"), False) and (os.getenv("LAVALINK_MODE", "off") or "off").strip().lower() != "off"
    nodelink_enabled = _as_bool(os.getenv("NODELINK_ENABLED"), False)

    if provider == "nodelink" or (provider == "auto" and nodelink_enabled):
        if not nodelink_enabled:
            return None
        return {
            "label": "NodeLink",
            "host": os.getenv("NODELINK_HOST", "127.0.0.1"),
            "port": _as_int(os.getenv("NODELINK_PORT"), 8787),
            "password": os.getenv("NODELINK_PASSWORD") or os.getenv("LAVALINK_PASSWORD", ""),
            "secure": _as_bool(os.getenv("NODELINK_SECURE"), False),
        }

    if not lavalink_enabled:
        return None
    return {
        "label": "Lavalink",
        "host": os.getenv("LAVALINK_HOST", "127.0.0.1"),
        "port": _as_int(os.getenv("LAVALINK_PORT"), 2333),
        "password": os.getenv("LAVALINK_PASSWORD", ""),
        "secure": _as_bool(os.getenv("LAVALINK_SECURE"), False),
    }


def _try_endpoint(url: str, password: str, timeout: float) -> tuple[bool, str]:
    headers = {"Accept": "application/json", "Client-Name": "tts-bot-audio-node-wait/1.0"}
    if password:
        headers["Authorization"] = password
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL local/env controlada
            body = resp.read(100_000).decode("utf-8", "replace")
            if 200 <= int(resp.status) < 300:
                # /version pode retornar texto puro; /v4/info retorna JSON.
                if body.strip().startswith("{"):
                    with contextlib_suppress_json():
                        json.loads(body)
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}: {body[:120]}"
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        return False, f"HTTP {exc.code}: {body[:160]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class contextlib_suppress_json:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return exc_type in {json.JSONDecodeError, ValueError}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/home/ubuntu/bot/.env")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--required", choices=["true", "false"], default=None)
    args = parser.parse_args()

    _load_env_file(Path(args.env))
    node = _selected_node()
    if node is None:
        print("[audio-node-wait] node de áudio desativado; seguindo sem espera")
        return 0

    required = _as_bool(args.required if args.required is not None else os.getenv("AUDIO_NODE_STARTUP_WAIT_REQUIRED"), True)
    timeout = args.timeout
    if timeout is None:
        timeout = _as_float(os.getenv("AUDIO_NODE_STARTUP_WAIT_SECONDS"), 90.0)
    timeout = max(0.0, float(timeout or 0.0))
    interval = max(0.5, float(args.interval or 2.0))

    label = str(node["label"])
    base = _base_url(str(node["host"]), int(node["port"]), bool(node["secure"]))
    password = str(node.get("password") or "")
    if not password:
        print(f"[audio-node-wait] {label} selecionado, mas senha ausente")
        return 1 if required else 0

    deadline = time.monotonic() + timeout
    attempts = 0
    last = "sem tentativa"
    paths = ["/v4/info", "/version"]
    while True:
        attempts += 1
        for path in paths:
            ok, detail = _try_endpoint(f"{base}{path}", password, timeout=min(8.0, max(2.0, interval + 1.0)))
            last = f"{path} -> {detail}"
            if ok:
                print(f"[audio-node-wait] {label} pronto em {base} após {attempts} tentativa(s): {last}")
                return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    print(f"[audio-node-wait] {label} não ficou pronto em {timeout:.0f}s ({base}); último resultado: {last}")
    return 1 if required else 0


if __name__ == "__main__":
    raise SystemExit(main())
