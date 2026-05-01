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
    effect: str
    recommendations: list[str]
    tests: list[str]


@dataclass
class HistoryItem:
    """Snapshot leve de um patch anterior — alimenta o prompt da próxima
    análise pra evitar repetição de tentativa."""
    created_at: float
    label: str
    changed_files: list[str]
    summary: str
    cause: str
    risk: str


class PatchBuilder:
    def __init__(self, repo_root: Path, output_dir: Path, *, max_files: int = 5, max_file_bytes: int = 220_000):
        self.repo_root = repo_root.resolve()
        self.output_dir = output_dir
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        # Histórico fica fora de output_dir pra não ir junto pro git.
        self.history_path = output_dir.parent / "patch_history.jsonl"

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip(" -•") for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                text = str(item).strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    def parse_ai_json(self, raw_text: str) -> dict[str, Any]:
        """Tenta extrair JSON da resposta da IA mesmo quando vem com fences ou
        texto antes/depois. Levanta RuntimeError se mesmo assim não decodifica."""
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

    def _detect_truncation(self, content: str, before: str | None, rel_path: str) -> str | None:
        """Detecta se a IA devolveu arquivo truncado/incompleto.

        Retorna mensagem de erro se suspeito, ou None se ok. Modelos médios
        (e às vezes Pro também) têm o vício de:
        - Cortar o arquivo no meio quando atinge limite de output
        - Substituir parte do código por `# ... resto inalterado ...`
        - Usar `pass` ou `...` como placeholder no meio de uma função

        Esses casos NUNCA devem ser aplicados — quebra o bot inteiro.
        """
        # Marcadores explícitos de "código omitido" — a IA é viciada nisso.
        # Lista de padrões observados em respostas reais de Gemini/Qwen/Llama.
        suspicious_markers = [
            "# ... resto",
            "# ... rest",
            "# ... código original",
            "# ... unchanged",
            "# ... (omitido",
            "# ... (omitted",
            "# resto do arquivo",
            "# restante do arquivo",
            "# resto do código",
            "# (mesmo conteúdo",
            "# same as before",
            "# code unchanged",
            "// ... resto",
            "/* ... resto",
            "<!-- ... -->",
        ]
        lower = content.lower()
        for marker in suspicious_markers:
            if marker.lower() in lower:
                return (
                    f"arquivo {rel_path} contém marcador de código omitido "
                    f"({marker!r}) — a IA cortou o conteúdo. Patch rejeitado."
                )

        # Detecta encolhimento drástico vs original. Se arquivo novo tem
        # <70% das linhas do original (sendo o original >100 linhas),
        # quase certeza que foi truncado. Limite generoso pra não bloquear
        # refatorações reais que encolhem código.
        if before is not None and len(before) > 1000:
            before_lines = before.count("\n") + 1
            content_lines = content.count("\n") + 1
            if before_lines >= 100 and content_lines < before_lines * 0.7:
                return (
                    f"arquivo {rel_path} encolheu drasticamente: "
                    f"{before_lines} → {content_lines} linhas "
                    f"({content_lines * 100 // before_lines}%). "
                    f"Provavelmente foi truncado pela IA. Patch rejeitado."
                )

        # Detecta `...` solto como statement Python (Ellipsis usado como
        # placeholder em meio de função). É sintaticamente válido mas
        # quase sempre é IA truncando.
        # Padrão: linha que tem só "..." ou "    ..." ou "        ..." (apenas
        # whitespace + ellipsis). Aceita 1 ocorrência se for em function stub
        # válido (como em protocols/ABC), mas 3+ é red flag.
        ellipsis_lines = sum(1 for line in content.splitlines() if line.strip() == "...")
        if ellipsis_lines >= 3:
            return (
                f"arquivo {rel_path} tem {ellipsis_lines} linhas com '...' como "
                f"placeholder — provavelmente truncamento da IA. Patch rejeitado."
            )

        return None

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
                # IMPORTANTE: detecção de truncamento ANTES de comparar com
                # original. Se a IA cortou o arquivo, o conteúdo "novo" pode
                # não ser igual ao original, passar de `before == content`,
                # e a gente acabar gravando lixo. Bloqueia aqui.
                source_path = safe_join(self.repo_root, rel)
                before = source_path.read_text("utf-8", errors="replace") if source_path.exists() else None
                truncation_msg = self._detect_truncation(content, before, rel.as_posix())
                if truncation_msg is not None:
                    raise RuntimeError(truncation_msg)
                # NÃO aplica redact_secrets no content do arquivo — isso muda
                # o código real que vai pro disco. redact_secrets é pra logs/
                # prompts da IA, não pra arquivos físicos. Se o original tinha
                # webhook_url hardcoded (não deveria, mas), preserva como está.
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
                "label": label,
                "zip": zip_path.name,
                "changed_files": changed,
                "provider_summary": str(data.get("summary") or "")[:1000],
                "cause": str(data.get("cause") or data.get("analysis") or "")[:1000],
                "risk": str(data.get("risk") or "médio")[:80],
                "effect": str(data.get("effect") or data.get("what_it_does") or data.get("impact") or "")[:1000],
                "recommendations": self._string_list(data.get("recommendations") or data.get("next_steps") or [])[:8],
                "tests": self._string_list(data.get("tests") or data.get("tests_to_run") or data.get("validation") or [])[:8],
            }
            with self.history_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(history_item, ensure_ascii=False) + "\n")

            return BuiltPatch(
                zip_path=zip_path,
                changed_files=changed,
                validation=validation,
                summary=str(data.get("summary") or data.get("fix_summary") or "Patch gerado pela DevAI.").strip(),
                cause=str(data.get("cause") or data.get("analysis") or "Causa não informada pela IA.").strip(),
                risk=str(data.get("risk") or "médio").strip(),
                effect=str(data.get("effect") or data.get("what_it_does") or data.get("impact") or "Não informado pela IA.").strip(),
                recommendations=self._string_list(data.get("recommendations") or data.get("next_steps") or [])[:8],
                tests=self._string_list(data.get("tests") or data.get("tests_to_run") or data.get("validation") or [])[:8],
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # --------------------------------------------------------------- history

    def recent_history(self, *, limit: int = 5, max_age_seconds: int = 7 * 24 * 3600) -> list[HistoryItem]:
        """Lê os últimos `limit` patches do `patch_history.jsonl`. Usado pelo
        cog pra avisar a IA o que ela mesma tentou recentemente.

        Filtra entradas mais antigas que `max_age_seconds` (padrão 7 dias)
        pra não poluir o prompt com história irrelevante.
        """
        if not self.history_path.exists():
            return []
        cutoff = time.time() - max_age_seconds
        out: list[HistoryItem] = []
        try:
            # Lê só a cauda do arquivo — basta pra `limit` linhas.
            with self.history_path.open("rb") as fp:
                fp.seek(0, os.SEEK_END)
                size = fp.tell()
                # 32KB normalmente é suficiente pros últimos ~30 patches.
                read_bytes = min(size, 32_768)
                fp.seek(size - read_bytes)
                tail = fp.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            created = float(item.get("created_at") or 0.0)
            if created < cutoff:
                continue
            out.append(
                HistoryItem(
                    created_at=created,
                    label=str(item.get("label") or "")[:80],
                    changed_files=list(item.get("changed_files") or [])[:12],
                    summary=str(item.get("provider_summary") or "")[:400],
                    cause=str(item.get("cause") or "")[:400],
                    risk=str(item.get("risk") or "")[:40],
                )
            )
            if len(out) >= limit:
                break
        return out
