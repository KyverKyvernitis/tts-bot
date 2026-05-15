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
    parser.add_argument("command", choices=["health", "status", "sha256", "zip", "zip-validate", "maintenance-plan", "log-extract", "log-summary", "text-stats", "ffprobe", "ffmpeg-convert"])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--host", default=env.get("PHONE_WORKER_HOST") or env.get("AUX_LAVALINK_HOST") or "")
    parser.add_argument("--port", default=env.get("PHONE_WORKER_PORT", "8766"))
    parser.add_argument("--token", default=env.get("PHONE_WORKER_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("-o", "--output", default="")
    parser.add_argument("--output-ext", default="")
    parser.add_argument("--input-ext", default="")
    parser.add_argument("--ffmpeg-arg", action="append", default=[])
    parser.add_argument("--max-lines", type=int, default=120)
    parser.add_argument("--max-recent", type=int, default=12)
    parser.add_argument("--max-top", type=int, default=12)
    parser.add_argument("--scan-root", default="/home/ubuntu/bot")
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

    if args.command == "zip-validate":
        if not args.paths:
            raise SystemExit("informe um arquivo .zip")
        src = Path(args.paths[0])
        payload = {
            "task": "zip_validate",
            "filename": src.name,
            "data_b64": base64.b64encode(src.read_bytes()).decode("ascii"),
        }
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "maintenance-plan":
        root = Path(args.paths[0] if args.paths else args.scan_root)
        kinds = {
            "tmp_audio": [root / "tmp_audio"],
            "log": [root / "logs"],
            "cache": [root / "tmp_audio" / "cache"],
        }
        entries = []
        for kind, dirs in kinds.items():
            for base_dir in dirs:
                if not base_dir.exists():
                    continue
                for item in base_dir.rglob("*"):
                    try:
                        if not item.is_file():
                            continue
                        st = item.stat()
                        entries.append({"path": str(item), "size": st.st_size, "mtime": st.st_mtime, "kind": kind})
                    except Exception:
                        continue
        payload = {"task": "maintenance_plan", "entries": entries}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "log-extract":
        text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in args.paths) if args.paths else sys.stdin.read()
        payload = {"task": "log_extract", "text": text, "max_lines": args.max_lines}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0


    if args.command == "log-summary":
        text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in args.paths) if args.paths else sys.stdin.read()
        payload = {"task": "log_summary", "text": text, "max_recent": args.max_recent, "max_top": args.max_top}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "ffprobe":
        if not args.paths:
            raise SystemExit("informe o arquivo de entrada")
        src = Path(args.paths[0])
        data = src.read_bytes()
        input_ext = (args.input_ext or src.suffix.lstrip(".") or "bin").strip(".")
        payload = {"task": "ffprobe_media", "data_b64": base64.b64encode(data).decode("ascii"), "input_ext": input_ext}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "text-stats":
        text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in args.paths) if args.paths else sys.stdin.read()
        payload = {"task": "text_stats", "text": text}
        print(json.dumps(request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout), indent=2, ensure_ascii=False))
        return 0

    if args.command == "ffmpeg-convert":
        if not args.paths:
            raise SystemExit("informe o arquivo de entrada")
        src = Path(args.paths[0])
        data = src.read_bytes()
        output_ext = (args.output_ext or (Path(args.output).suffix.lstrip(".") if args.output else "ogg") or "ogg").strip(".")
        input_ext = (args.input_ext or src.suffix.lstrip(".") or "bin").strip(".")
        payload = {
            "task": "ffmpeg_convert",
            "data_b64": base64.b64encode(data).decode("ascii"),
            "input_ext": input_ext,
            "output_ext": output_ext,
        }
        if args.ffmpeg_arg:
            payload["ffmpeg_args"] = args.ffmpeg_arg
        result = request_json(f"{base}/task", args.token, payload=payload, timeout=args.timeout)
        out_data = base64.b64decode(result.pop("data_b64"))
        out_path = Path(args.output or f"{src.stem}.phone-worker.{result.get('output_ext') or output_ext}")
        out_path.write_bytes(out_data)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"salvo em: {out_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
