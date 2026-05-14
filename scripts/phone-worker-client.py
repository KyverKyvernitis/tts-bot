#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request_json(url: str, token: str, *, payload: dict | None = None, timeout: float = 10.0) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise SystemExit(f"HTTP {exc.code}: {raw.decode('utf-8', errors='ignore')}") from exc
    return json.loads(raw.decode("utf-8"))


def main() -> int:
    env = read_env(Path(os.getenv("ENV_FILE", "/home/ubuntu/bot/.env")))
    parser = argparse.ArgumentParser(description="Cliente simples do phone-worker.")
    parser.add_argument("command", choices=["health", "status", "sha256", "zip", "log-extract"])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--host", default=env.get("PHONE_WORKER_HOST") or env.get("AUX_LAVALINK_HOST") or "")
    parser.add_argument("--port", default=env.get("PHONE_WORKER_PORT", "8766"))
    parser.add_argument("--token", default=env.get("PHONE_WORKER_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    if not args.host:
        raise SystemExit("PHONE_WORKER_HOST não configurado")
    base = f"http://{args.host}:{args.port}"

    if args.command in {"health", "status"}:
        print(json.dumps(request_json(f"{base}/{args.command if args.command == 'status' else 'health'}", args.token, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "sha256":
        if not args.paths:
            raise SystemExit("informe um arquivo")
        data = Path(args.paths[0]).read_bytes()
        payload = {"task": "sha256", "data_b64": base64.b64encode(data).decode("ascii")}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "zip":
        if not args.paths:
            raise SystemExit("informe arquivos")
        files = []
        for item in args.paths:
            path = Path(item)
            files.append({"name": path.name, "data_b64": base64.b64encode(path.read_bytes()).decode("ascii")})
        payload = {"task": "zip", "filename": "phone-worker.zip", "files": files}
        result = request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout)
        data = base64.b64decode(result.pop("data_b64"))
        out = Path(result.get("filename") or "phone-worker.zip")
        out.write_bytes(data)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"salvo em: {out}")
        return 0

    if args.command == "log-extract":
        text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in args.paths) if args.paths else sys.stdin.read()
        payload = {"task": "log_extract", "text": text}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
