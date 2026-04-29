from __future__ import annotations

import json
import os
import py_compile
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import is_safe_patch_path, normalize_rel_path, redact_secrets, safe_join


@dataclass
class BuiltPatch:
    zip_path: Path
    changed_files: list[str]
    validation: list[str]
    summary: str
    cause: str
    risk: str


class PatchBuilder:
    def __init__(self, repo_root: Path, output_dir: Path, *, max_files: int = 5, max_file_bytes: int = 220_000):
        self.repo_root = repo_root.resolve()
        self.output_dir = output_dir
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def parse_ai_json(self, raw_text: str) -> dict[str, Any]:
        text = (raw_text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"a IA não devolveu JSON válido: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("resposta JSON da IA não é um objeto")
        return data

    def build_from_ai_response(self, raw_text: str, *, label: str = "auto") -> BuiltPatch:
        data = self.parse_ai_json(raw_text)
        files = data.get("files") or data.get("changed_files") or []
        if not isinstance(files, list) or not files:
            raise RuntimeError("a IA não retornou nenhum arquivo corrigido")
        if len(files) > self.max_files:
            raise RuntimeError(f"patch recusado: {len(files)} arquivos excede limite {self.max_files}")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="devai-patch-"))
        changed: list[str] = []
        validation: list[str] = []
        try:
            for item in files:
                if not isinstance(item, dict):
                    raise RuntimeError("entrada de arquivo inválida na resposta da IA")
                rel = normalize_rel_path(str(item.get("path") or item.get("file") or ""))
                if not is_safe_patch_path(rel):
                    raise RuntimeError(f"patch recusado para caminho não permitido: {rel.as_posix()}")
                content = item.get("content")
                if content is None:
                    raise RuntimeError(f"arquivo sem content: {rel.as_posix()}")
                if not isinstance(content, str):
                    content = str(content)
                if len(content.encode("utf-8")) > self.max_file_bytes:
                    raise RuntimeError(f"arquivo grande demais para patch automático: {rel.as_posix()}")
                # Não deixa a IA gravar secrets acidentalmente em arquivo novo.
                content = redact_secrets(content)
                source_path = safe_join(self.repo_root, rel)
                before = source_path.read_text("utf-8", errors="replace") if source_path.exists() else None
                if before == content:
                    continue
                out_path = temp_dir / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, "utf-8")
                if rel.suffix.lower() == ".py":
                    py_compile.compile(str(out_path), doraise=True)
                    validation.append(f"Syntax OK: {rel.as_posix()}")
                changed.append(rel.as_posix())

            if not changed:
                raise RuntimeError("a IA retornou arquivos, mas nada mudou em relação à base atual")

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            zip_path = self.output_dir / f"patch_devai_{label}_{timestamp}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for rel_name in changed:
                    zf.write(temp_dir / rel_name, arcname=rel_name)

            history_item = {
                "created_at": time.time(),
                "zip": zip_path.name,
                "changed_files": changed,
                "provider_summary": str(data.get("summary") or "")[:1000],
                "cause": str(data.get("cause") or data.get("analysis") or "")[:1000],
                "risk": str(data.get("risk") or "médio")[:80],
            }
            history_path = self.output_dir.parent / "patch_history.jsonl"
            with history_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(history_item, ensure_ascii=False) + "\n")

            return BuiltPatch(
                zip_path=zip_path,
                changed_files=changed,
                validation=validation,
                summary=str(data.get("summary") or data.get("fix_summary") or "Patch gerado pela DevAI.").strip(),
                cause=str(data.get("cause") or data.get("analysis") or "Causa não informada pela IA.").strip(),
                risk=str(data.get("risk") or "médio").strip(),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
