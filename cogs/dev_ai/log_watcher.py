from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .safety import redact_secrets

TRACEBACK_START_RE = re.compile(r"Traceback \(most recent call last\):")
ERROR_LINE_RE = re.compile(r"(?i)(ERROR|CRITICAL|Exception|Traceback|discord\.errors|JSONDecodeError|ClientException|RuntimeError|Forbidden|HTTPException)")
PYERR_END_RE = re.compile(r"^[A-Za-z_][\w.]*?(Error|Exception|Warning):\s+.+")
FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+), in ([\w_<>]+)')


@dataclass
class LogEvent:
    source: str
    text: str
    signature: str
    file_paths: list[str]
    created_at: float


class LogWatcher:
    def __init__(self, repo_root: Path, log_paths: list[Path], *, max_lines: int = 180, scan_existing: bool = False):
        self.repo_root = repo_root.resolve()
        self.log_paths = log_paths
        self.max_lines = max_lines
        self.offsets: dict[Path, int] = {}
        self.recent_signatures: dict[str, float] = {}
        self.scan_existing = scan_existing
        self._initialized = False

    def _existing_paths(self) -> list[Path]:
        result: list[Path] = []
        for raw in self.log_paths:
            if "*" in raw.as_posix():
                result.extend(sorted(raw.parent.glob(raw.name)))
            elif raw.exists():
                result.append(raw)
        return [p for p in result if p.is_file()]

    def initialize(self) -> None:
        for path in self._existing_paths():
            try:
                self.offsets[path] = 0 if self.scan_existing else path.stat().st_size
            except OSError:
                pass
        self._initialized = True

    def poll(self) -> list[LogEvent]:
        if not self._initialized:
            self.initialize()
        events: list[LogEvent] = []
        for path in self._existing_paths():
            try:
                size = path.stat().st_size
                old = self.offsets.get(path, 0)
                if size < old:
                    old = 0
                if size == old:
                    continue
                with path.open("rb") as fp:
                    fp.seek(old)
                    data = fp.read(min(size - old, 256_000))
                self.offsets[path] = size
                text = data.decode("utf-8", errors="replace")
                events.extend(self._extract_events(path, text))
            except OSError:
                continue
        return events

    def _extract_events(self, path: Path, text: str) -> list[LogEvent]:
        lines = text.splitlines()
        blocks: list[list[str]] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if TRACEBACK_START_RE.search(line):
                block = [line]
                i += 1
                while i < len(lines) and len(block) < self.max_lines:
                    block.append(lines[i])
                    if PYERR_END_RE.search(lines[i]):
                        break
                    i += 1
                blocks.append(block)
            elif ERROR_LINE_RE.search(line):
                start = max(0, i - 6)
                end = min(len(lines), i + 22)
                blocks.append(lines[start:end])
            i += 1

        events: list[LogEvent] = []
        for block in blocks:
            raw_text = "\n".join(block[-self.max_lines:]).strip()
            if not raw_text:
                continue
            clean = redact_secrets(raw_text, max_chars=18000)
            signature = hashlib.sha256(self._normalize_for_signature(clean).encode("utf-8", errors="replace")).hexdigest()[:16]
            now = time.time()
            last = self.recent_signatures.get(signature, 0)
            if now - last < 900:
                continue
            self.recent_signatures[signature] = now
            events.append(LogEvent(
                source=path.as_posix(),
                text=clean,
                signature=signature,
                file_paths=self._extract_repo_paths(clean),
                created_at=now,
            ))
        return events

    def _normalize_for_signature(self, text: str) -> str:
        value = re.sub(r"\bline \d+\b", "line N", text)
        value = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", value)
        value = re.sub(r"\d{2}:\d{2}:\d{2}", "HH:MM:SS", value)
        return value[-5000:]

    def _extract_repo_paths(self, text: str) -> list[str]:
        found: list[str] = []
        for match in FILE_LINE_RE.finditer(text):
            raw = match.group(1)
            try:
                p = Path(raw)
                rel = p.resolve().relative_to(self.repo_root) if p.is_absolute() else p
                posix = rel.as_posix()
                if posix.endswith(".py") and posix not in found:
                    found.append(posix)
            except Exception:
                continue
        return found[:8]
