from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import BLOCKED_PARTS, is_safe_to_read, redact_secrets, safe_join


@dataclass
class ProjectFileSummary:
    path: str
    kind: str
    classes: list[str]
    functions: list[str]
    commands: list[str]
    imports: list[str]
    lines: int


class ProjectIndexer:
    def __init__(self, repo_root: Path, data_dir: Path, *, max_files: int = 260):
        self.repo_root = repo_root.resolve()
        self.data_dir = data_dir
        self.max_files = max_files
        self.index_path = data_dir / "project_index.json"

    def _iter_project_files(self):
        allowed_suffixes = {".py", ".sh", ".txt", ".md", ".json", ".yaml", ".yml", ".toml"}
        yielded = 0
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.repo_root)
            if any(part in BLOCKED_PARTS for part in rel.parts):
                continue
            if rel.parts and rel.parts[0] in {"logs", "tmp", "tmp_audio"}:
                continue
            if rel.parts[:2] == ("data", "dev_ai"):
                continue
            if rel.suffix.lower() not in allowed_suffixes and rel.name != "Dockerfile":
                continue
            if not is_safe_to_read(rel):
                continue
            yield path, rel
            yielded += 1
            if yielded >= self.max_files:
                return

    def _summarize_python(self, path: Path, rel: Path) -> ProjectFileSummary:
        classes: list[str] = []
        functions: list[str] = []
        commands: list[str] = []
        imports: list[str] = []
        try:
            text = path.read_text("utf-8", errors="replace")
            tree = ast.parse(text)
        except Exception:
            text = path.read_text("utf-8", errors="replace") if path.exists() else ""
            return ProjectFileSummary(rel.as_posix(), "python", [], [], [], [], len(text.splitlines()))

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
                for dec in node.decorator_list:
                    dec_text = self._decorator_name(dec)
                    if dec_text and ("command" in dec_text.lower() or "listener" in dec_text.lower()):
                        commands.append(f"{node.name} @{dec_text}")
            elif isinstance(node, ast.Import):
                for alias in node.names[:4]:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = "." * int(node.level or 0) + (node.module or "")
                imports.append(mod)
        return ProjectFileSummary(
            path=rel.as_posix(),
            kind="python",
            classes=classes[:30],
            functions=functions[:50],
            commands=commands[:40],
            imports=imports[:30],
            lines=len(text.splitlines()),
        )

    def _decorator_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._decorator_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return self._decorator_name(node.func)
        return ""

    def build_index(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        for path, rel in self._iter_project_files():
            try:
                if rel.suffix.lower() == ".py":
                    summary = self._summarize_python(path, rel)
                    files.append(summary.__dict__)
                else:
                    text = path.read_text("utf-8", errors="replace")[:4000]
                    files.append({
                        "path": rel.as_posix(),
                        "kind": rel.suffix.lower().lstrip(".") or rel.name,
                        "classes": [],
                        "functions": [],
                        "commands": [],
                        "imports": [],
                        "lines": len(text.splitlines()),
                    })
            except Exception:
                continue

        index = {
            "generated_at": time.time(),
            "repo_root": str(self.repo_root),
            "file_count": len(files),
            "files": files,
        }
        self.index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), "utf-8")
        return index

    def load_or_build(self, *, max_age_seconds: int = 1800) -> dict[str, Any]:
        try:
            if self.index_path.exists() and (time.time() - self.index_path.stat().st_mtime) <= max_age_seconds:
                return json.loads(self.index_path.read_text("utf-8"))
        except Exception:
            pass
        return self.build_index()

    def compact_context(self, index: dict[str, Any], *, max_chars: int = 12000) -> str:
        lines = [f"Projeto: {index.get('file_count', 0)} arquivos indexados"]
        for item in index.get("files", [])[:220]:
            path = item.get("path")
            kind = item.get("kind")
            funcs_list = item.get("functions") or []
            classes_list = item.get("classes") or []
            commands_list = item.get("commands") or []
            detail = []
            if classes_list:
                detail.append("classes=" + ",".join(classes_list[:8]))
            if commands_list:
                detail.append("commands=" + ",".join(commands_list[:8]))
            elif funcs_list:
                detail.append("funcs=" + ",".join(funcs_list[:8]))
            suffix = " | " + " | ".join(detail) if detail else ""
            lines.append(f"- {path} ({kind}, {item.get('lines', 0)} linhas){suffix}")
        text = "\n".join(lines)
        return redact_secrets(text[:max_chars], max_chars=max_chars)

    def read_files_for_context(self, rel_paths: list[str], *, max_files: int, max_chars_per_file: int) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw in rel_paths[:max_files]:
            try:
                rel = Path(str(raw).replace("\\", "/"))
                if not is_safe_to_read(rel):
                    continue
                path = safe_join(self.repo_root, rel)
                if not path.exists() or not path.is_file():
                    continue
                text = path.read_text("utf-8", errors="replace")
                if len(text) > max_chars_per_file:
                    text = text[: max_chars_per_file // 2] + "\n\n# ... trecho central omitido ...\n\n" + text[-max_chars_per_file // 2 :]
                result[rel.as_posix()] = redact_secrets(text, max_chars=max_chars_per_file)
            except Exception:
                continue
        return result
