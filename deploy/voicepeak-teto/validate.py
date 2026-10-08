#!/usr/bin/env python3
"""Check a configured licensed VOICEPEAK host and optionally save a real WAV."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT, ROOT / "deploy" / "termux" / "phone-worker"):
    sys.path.insert(0, str(directory))
from phone_worker_runtime.tts_providers import VoicepeakRenderer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-test", action="store_true", help="Sintetizar e salvar uma amostra real")
    parser.add_argument("--text", default="Olá, eu sou a Teto. Como você está hoje?")
    parser.add_argument("--output", type=Path, default=Path("teto-voicepeak-teste.wav"))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("--timeout deve estar entre 0 e 120 segundos")
    deadline = time.monotonic() + args.timeout
    renderer = VoicepeakRenderer()
    status = renderer.status(force=True, timeout_seconds=args.timeout)
    report = {key: status.get(key) for key in
              ("ready", "backend", "source", "narrator", "reading_mode", "renderer_version", "last_error")}
    if not status.get("ready"):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if args.render_test:
        try:
            result = renderer.synthesize(args.text, timeout_seconds=deadline - time.monotonic())
            args.output.write_bytes(result["audio"])
            report.update(output=str(args.output), audio_seconds=result.get("audio_seconds"),
                          reading_text=result.get("reading_text"),
                          experimental_pronunciation=result.get("experimental_pronunciation"))
        except Exception as exc:
            report.update(ready=False, last_error=str(exc))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
