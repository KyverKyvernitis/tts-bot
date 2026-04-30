"""Indexador do projeto.

Faz um snapshot leve do repositório (no máximo `max_files`) com:

- AST de cada .py: classes, funções, comandos, imports e **primeira linha
  da docstring do módulo** (essa é a mudança principal desta versão — sem
  ela a IA só vê nomes, não propósito).
- Grafo reverso de imports (`importers`): quando um arquivo é importado por
  outros, lista quais. Isso permite à IA saber que mexer em `cogs/tts/audio.py`
  pode quebrar `cogs/tts/cog.py`.
- Para arquivos não-Python (config, sh, json, md), guarda contagem de linhas
  e a primeira linha não-vazia como "purpose".

A saída fica em `data/dev_ai/project_index.json` e é lida na hora de montar
o prompt. O cog re-roda `build_index()` depois de cada patch aplicado.
"""
from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass, field
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
    purpose: str = ""
    importers: list[str] = field(default_factory=list)


class ProjectIndexer:
    def __init__(self, repo_root: Path, data_dir: Path, *, max_files: int = 260):
        self.repo_root = repo_root.resolve()
        self.data_dir = data_dir
        self.max_files = max_files
        self.index_path = data_dir / "project_index.json"

    # ------------------------------------------------------------------- iter

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

    # --------------------------------------------------------------- summary

    def _summarize_python(self, path: Path, rel: Path) -> ProjectFileSummary:
        classes: list[str] = []
        functions: list[str] = []
        commands: list[str] = []
        imports: list[str] = []
        purpose = ""
        try:
            text = path.read_text("utf-8", errors="replace")
            tree = ast.parse(text)
        except Exception:
            text = path.read_text("utf-8", errors="replace") if path.exists() else ""
            return ProjectFileSummary(rel.as_posix(), "python", [], [], [], [], len(text.splitlines()))

        # Docstring do módulo: pega a 1ª linha pra sintetizar propósito.
        module_doc = ast.get_docstring(tree, clean=True) or ""
        if module_doc:
            purpose = module_doc.splitlines()[0].strip()[:200]

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
            purpose=purpose,
        )

    def _summarize_text(self, path: Path, rel: Path) -> ProjectFileSummary:
        try:
            text = path.read_text("utf-8", errors="replace")
        except Exception:
            text = ""
        purpose = ""
        for line in text.splitlines()[:10]:
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                purpose = stripped[:200]
                break
        return ProjectFileSummary(
            path=rel.as_posix(),
            kind=rel.suffix.lower().lstrip(".") or rel.name,
            classes=[],
            functions=[],
            commands=[],
            imports=[],
            lines=len(text.splitlines()),
            purpose=purpose,
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

    # -------------------------------------------------------- index building

    def _build_import_graph(self, summaries: list[ProjectFileSummary]) -> None:
        """Preenche `summaries[i].importers` com paths que importam esse módulo.

        Heurística simples: olha o último componente do path (sem .py) e os
        prefixos de pacote, e marca quem tem `import` ou `from ... import`
        que case com isso. Não é perfeito (não resolve relative imports), mas
        é o suficiente pra IA saber 'este arquivo é usado por X, Y, Z'.
        """
        # Constrói um índice path -> conjunto de "tokens" que outros arquivos
        # podem importar pra alcançar este módulo.
        token_to_paths: dict[str, list[str]] = {}
        for summary in summaries:
            if summary.kind != "python":
                continue
            posix = summary.path
            # ex: "cogs/tts/cog.py" -> tokens: "cogs.tts.cog", "tts.cog", "cog"
            without_ext = posix[:-3] if posix.endswith(".py") else posix
            parts = without_ext.split("/")
            for size in range(1, len(parts) + 1):
                token = ".".join(parts[-size:])
                token_to_paths.setdefault(token, []).append(posix)

        for summary in summaries:
            if summary.kind != "python":
                continue
            for imp in summary.imports:
                norm = imp.lstrip(".")
                if not norm:
                    continue
                for target_path in token_to_paths.get(norm, []):
                    if target_path == summary.path:
                        continue
                    target_summary = next((s for s in summaries if s.path == target_path), None)
                    if target_summary and summary.path not in target_summary.importers:
                        target_summary.importers.append(summary.path)

        # limita pra não estourar o JSON.
        for summary in summaries:
            summary.importers = summary.importers[:12]

    def build_index(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[ProjectFileSummary] = []
        for path, rel in self._iter_project_files():
            try:
                if rel.suffix.lower() == ".py":
                    summaries.append(self._summarize_python(path, rel))
                else:
                    summaries.append(self._summarize_text(path, rel))
            except Exception:
                continue

        self._build_import_graph(summaries)

        index = {
            "generated_at": time.time(),
            "repo_root": str(self.repo_root),
            "file_count": len(summaries),
            "files": [s.__dict__ for s in summaries],
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

    # ----------------------------------------------------- contextualization

    def compact_context(self, index: dict[str, Any], *, max_chars: int = 12000) -> str:
        """Resumo textual otimizado pra IA — inclui propósito e quem usa cada
        arquivo. Modelos pequenos se beneficiam muito de ter o "porquê" do
        arquivo, não só os nomes."""
        lines = [f"Projeto: {index.get('file_count', 0)} arquivos indexados"]
        for item in index.get("files", [])[:240]:
            path = item.get("path")
            kind = item.get("kind")
            funcs_list = item.get("functions") or []
            classes_list = item.get("classes") or []
            commands_list = item.get("commands") or []
            importers = item.get("importers") or []
            purpose = (item.get("purpose") or "").strip()

            head = f"- {path} ({kind}, {item.get('lines', 0)} linhas)"
            if purpose:
                head += f" — {purpose}"
            lines.append(head)

            detail_parts: list[str] = []
            if classes_list:
                detail_parts.append("classes=" + ",".join(classes_list[:8]))
            if commands_list:
                detail_parts.append("cmds=" + ",".join(commands_list[:6]))
            elif funcs_list:
                detail_parts.append("funcs=" + ",".join(funcs_list[:8]))
            if importers:
                detail_parts.append("usado_por=" + ",".join(importers[:6]))
            if detail_parts:
                lines.append("    " + " | ".join(detail_parts))

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

    def expand_related_files(self, seeds: list[str], index: dict[str, Any], *, max_total: int = 6) -> list[str]:
        """Dado um conjunto de arquivos do traceback, devolve a lista expandida
        com os imports e importers diretos. Usa o grafo construído em
        `build_index`. Útil pra montar contexto sem chutar palavras-chave."""
        files_by_path = {item["path"]: item for item in index.get("files", []) if isinstance(item, dict)}
        out: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if path and path not in seen and path in files_by_path:
                seen.add(path)
                out.append(path)

        for seed in seeds:
            _add(seed)
            item = files_by_path.get(seed)
            if not item:
                continue
            # importers diretos primeiro (quem chama esse arquivo)
            for importer in item.get("importers") or []:
                _add(importer)
                if len(out) >= max_total:
                    return out
        return out[:max_total]
