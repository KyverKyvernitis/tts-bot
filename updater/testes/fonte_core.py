"""Visão expandida do core para testes de contrato legados."""
from __future__ import annotations
import hashlib
import re
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "updater" / "core"
ENTRYPOINT = CORE / "atualizar.sh"
_SOURCE_RE = re.compile(r'^\s*(?:source|\.)\s+["\']?\$UPDATER_SOURCE_DIR/([^"\'\s]+)["\']?\s*$')

def ler_fonte_core() -> str:
    partes: list[str] = []
    for linha in ENTRYPOINT.read_text(encoding="utf-8").splitlines(keepends=True):
        match = _SOURCE_RE.match(linha.rstrip("\n"))
        if not match:
            partes.append(linha); continue
        modulo = CORE / match.group(1)
        if not modulo.is_file():
            partes.append(linha); continue
        conteudo = modulo.read_text(encoding="utf-8")
        partes.append(f"# --- início módulo {modulo.name} ---\n")
        partes.append(conteudo)
        if not conteudo.endswith("\n"): partes.append("\n")
        partes.append(f"# --- fim módulo {modulo.name} ---\n")
    return "".join(partes)

def caminho_fonte_core() -> Path:
    fonte = ler_fonte_core()
    digest = hashlib.sha256(fonte.encode("utf-8")).hexdigest()[:16]
    destino = Path(tempfile.gettempdir()) / f"tts-bot-updater-core-tests-{digest}.sh"
    if not destino.exists() or destino.read_text(encoding="utf-8") != fonte:
        destino.write_text(fonte, encoding="utf-8")
    return destino
