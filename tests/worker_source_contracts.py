"""Release contracts read from the physical entrypoint without starting runtime."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def phone_worker_version() -> str:
    source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PHONE_WORKER_VERSION" for t in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            assert len(value.split(".")) == 3 and all(part.isdigit() for part in value.split("."))
            return value
    raise AssertionError("PHONE_WORKER_VERSION must remain a literal in the physical entrypoint")


def phone_worker_version_tuple() -> tuple[int, ...]:
    return tuple(map(int, phone_worker_version().split(".")))
