"""Áudio temporário retomável até o Discord confirmar o arquivamento.

Não é um cache de reprodução: trabalhos concluídos removem todos os bytes e
trabalhos abandonados expiram. O lock impede que a limpeza toque um upload ativo.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

import fcntl


class ArchiveWorkspaceBusy(RuntimeError):
    pass


class ArchiveWorkspace:
    def __init__(self, key: str, identity: Any, *, root: Path | None = None,
                 ttl_seconds: float | None = None) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("chave inválida do trabalho temporário")
        self.root = Path(root) if root is not None else Path(tempfile.gettempdir()) / f"core-music-archive-{os.getuid()}"
        self.path = self.root / key
        self.identity = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":")).encode()).hexdigest()
        if ttl_seconds is None:
            try:
                ttl_seconds = float(os.getenv("MUSIC_AGENT_ARCHIVE_TEMP_TTL_SECONDS", "86400"))
            except ValueError:
                ttl_seconds = 86400.0
        self.ttl_seconds = max(60.0, min(7 * 86400.0, ttl_seconds))
        self._lock = None
        self._confirmed = False

    def __enter__(self) -> ArchiveWorkspace:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._cleanup_expired()
        self.path.mkdir(exist_ok=True, mode=0o700)
        self._lock = (self.path / ".lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            self._lock = None
            raise ArchiveWorkspaceBusy("arquivamento desta faixa já está em andamento") from exc
        try:
            state = self.path / ".identity"
            previous = state.read_text() if state.is_file() else ""
            if previous != self.identity:
                for child in self.path.iterdir():
                    if child.name == ".lock":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                state.write_text(self.identity)
            os.utime(self.path, None)
            return self
        except BaseException:
            self._release()
            raise

    @classmethod
    def cleanup_expired(cls, *, root: Path | None = None, ttl_seconds: float | None = None) -> int:
        """Limpa trabalhos abandonados mesmo quando não houver novos uploads."""
        workspace = cls("0" * 32, {}, root=root, ttl_seconds=ttl_seconds)
        if not workspace.root.is_dir():
            return 0
        return workspace._cleanup_expired(skip_current=False)

    def _cleanup_expired(self, *, skip_current: bool = True) -> int:
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        for folder in self.root.iterdir():
            try:
                if ((skip_current and folder == self.path) or folder.is_symlink() or not folder.is_dir()
                        or not re.fullmatch(r"[a-f0-9]{32}", folder.name) or folder.stat().st_mtime >= cutoff):
                    continue
                with (folder / ".lock").open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if folder.stat().st_mtime < cutoff:
                        shutil.rmtree(folder)
                        removed += 1
            except OSError:
                # Um trabalho ativo ou que mudou durante a inspeção fica intacto.
                continue
        return removed

    def load_reference(self) -> dict:
        try:
            value = json.loads((self.path / ".reference.json").read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_reference(self, reference: dict) -> None:
        temporary = self.path / ".reference.tmp"
        temporary.write_text(json.dumps(reference, separators=(",", ":"), allow_nan=False))
        temporary.replace(self.path / ".reference.json")
        os.utime(self.path, None)

    def complete(self) -> None:
        """Chamar somente depois da confirmação dos anexos e do manifesto."""
        self._confirmed = True

    def _release(self) -> None:
        if self._lock is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._confirmed:
                shutil.rmtree(self.path)
            elif self.path.exists():
                os.utime(self.path, None)
        finally:
            self._release()
