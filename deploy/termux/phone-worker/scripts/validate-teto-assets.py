#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_DIR = SCRIPT_DIR.parent
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer import TetoRenderer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Valida voicebank e resampler da Kasane Teto no phone worker.")
    parser.add_argument("--voicebank", required=True, help="Pasta da voicebank UTAU instalada pelo operador.")
    parser.add_argument(
        "--mode",
        choices=("standard", "english", "auto"),
        default="standard",
        help="Perfil da voicebank a validar. O padrão mantém compatibilidade e valida exatamente --voicebank.",
    )
    parser.add_argument("--resampler", required=True, help="Comando do resampler, por exemplo: python ~/bin/straycat.py")
    parser.add_argument("--render-test", action="store_true", help="Também sintetiza uma frase curta de teste.")
    parser.add_argument("--text", default="teto", help="Texto usado no teste opcional.")
    args = parser.parse_args()

    os.environ["PHONE_WORKER_TETO_ENABLED"] = "true"
    voicebank = str(Path(args.voicebank).expanduser())
    os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"] = args.mode
    if args.mode == "english":
        os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = voicebank
    elif args.mode == "auto":
        # In auto mode the supplied bank is treated as the English candidate,
        # while the normal configured standard bank remains available as fallback.
        os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = voicebank
    else:
        os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] = voicebank
    os.environ["PHONE_WORKER_TETO_RESAMPLER_COMMAND"] = args.resampler
    renderer = TetoRenderer(resource_guard=lambda: {"ok": True, "reason": "validator"})
    result = {"status": renderer.status(force=True)}
    if not result["status"].get("ready"):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if args.render_test:
        rendered = renderer.synthesize(args.text, timeout_seconds=30, max_audio_bytes=8 * 1024 * 1024)
        result["render_test"] = {
            key: value for key, value in rendered.items() if key != "audio"
        }
        result["render_test"]["bytes"] = len(rendered.get("audio") or b"")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
