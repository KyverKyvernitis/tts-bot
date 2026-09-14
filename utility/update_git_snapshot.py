"""Fachada de compatibilidade; implementação em updater.utilitarios.snapshot_git."""
from pathlib import Path
import sys
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path: sys.path.insert(0, str(_ROOT))
from updater.utilitarios.snapshot_git import *  # noqa: F401,F403,E402
from updater.utilitarios.snapshot_git import main as _main  # noqa: E402
if __name__ == "__main__":
    result = _main()
    if result is not None: raise SystemExit(result)
