from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .errors import TetoVoicebankError


@dataclass(frozen=True, slots=True)
class OtoEntry:
    alias: str
    wav_path: Path
    offset_ms: float
    consonant_ms: float
    cutoff_ms: float
    preutterance_ms: float
    overlap_ms: float

    def cache_identity(self) -> str:
        try:
            stat = self.wav_path.stat()
            stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            stamp = "missing"
        return "|".join((
            self.alias,
            str(self.wav_path),
            stamp,
            f"{self.offset_ms:.3f}",
            f"{self.consonant_ms:.3f}",
            f"{self.cutoff_ms:.3f}",
            f"{self.preutterance_ms:.3f}",
            f"{self.overlap_ms:.3f}",
        ))


def _decode_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def _number(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _normalized_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _pitch_number(value: str) -> int | None:
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?[0-9]{1,2})", str(value or "").strip())
    if match is None:
        return None
    semitone = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[match[1].upper()]
    semitone += 1 if match[2] == "#" else -1 if match[2] == "b" else 0
    number = (int(match[3]) + 1) * 12 + semitone
    return number if 0 <= number <= 127 else None


def _read_pitch_map(base: Path, digest) -> dict[int, tuple[str, str]]:
    path = base / "prefix.map"
    if not path.is_file():
        candidates = [candidate for candidate in sorted(base.rglob("prefix.map"))
                      if _is_within(candidate.resolve(), base)]
        if len(candidates) > 1:
            raise TetoVoicebankError("voicebank contém vários prefix.map; selecione a pasta do banco")
        path = candidates[0] if candidates else path
    if not path.is_file():
        raise TetoVoicebankError("voicebank multipitch sem prefix.map")
    if not _is_within(path.resolve(), base):
        raise TetoVoicebankError("prefix.map fora da pasta da voicebank")
    try:
        contents = path.read_bytes()
        text = _decode_text(path)
    except OSError as exc:
        raise TetoVoicebankError(f"não foi possível ler {path}: {exc}") from exc
    digest.update(b"pitch-map-v1\x00")
    digest.update(str(path.relative_to(base)).encode("utf-8", errors="replace"))
    digest.update(contents)
    result = {}
    for line in text.splitlines():
        fields = line.lstrip("\ufeff").split("\t")
        if len(fields) < 3:
            continue
        number = _pitch_number(fields[0])
        if number is None:
            continue
        # Affix spaces are meaningful in UTAU: unlike an alias, a prefix may
        # intentionally end with a space before the canonical phoneme.
        prefix, suffix = (unicodedata.normalize("NFKC", part) for part in fields[1:3])
        if any("\x00" in part or len(part) > 120 for part in (prefix, suffix)):
            raise TetoVoicebankError("prefix.map contém um prefixo ou sufixo inválido")
        result[number] = (prefix, suffix)
    if not result:
        raise TetoVoicebankError("prefix.map sem notas válidas")
    return result


class VoicebankIndex:
    def __init__(
        self, root: Path, entries: dict[str, OtoEntry], folded_entries: dict[str, OtoEntry],
        fingerprint: str, name: str, *, pitch_map: dict[int, tuple[str, str]] | None = None,
    ):
        self.root = root
        self.entries = entries
        self.folded_entries = folded_entries
        self.fingerprint = fingerprint
        self.name = name
        self.pitch_map = dict(pitch_map or {})
        registers: dict[tuple[str, str], list[int]] = {}
        for number, affixes in self.pitch_map.items():
            registers.setdefault(affixes, []).append(number)
        self._pitch_registers = {affixes: tuple(sorted(numbers))
                                 for affixes, numbers in registers.items()}

    @classmethod
    def load(
        cls, root: str | Path, *, minimum_aliases: int = 10,
        alias_suffix: str = "", case_sensitive: bool = False,
        pitch_map: bool = False,
    ) -> "VoicebankIndex":
        base = Path(root).expanduser().resolve()
        if not base.is_dir():
            raise TetoVoicebankError(f"voicebank não encontrada: {base}")

        oto_files = sorted(base.rglob("oto.ini"))
        if not oto_files:
            raise TetoVoicebankError("voicebank sem oto.ini")

        entries: dict[str, OtoEntry] = {}
        folded_candidates: dict[str, OtoEntry | None] = {}
        digest = hashlib.sha256()
        digest.update(str(base).encode("utf-8", errors="replace"))
        suffix = _normalized_alias(alias_suffix)
        if suffix and not suffix.startswith("_"):
            suffix = "_" + suffix
        if suffix or case_sensitive:
            digest.update(f"alias-map-v1:{suffix}:{int(case_sensitive)}".encode("utf-8"))
        mapped_pitches = _read_pitch_map(base, digest) if pitch_map else None

        for oto_path in oto_files:
            resolved_oto = oto_path.resolve()
            if not _is_within(resolved_oto, base):
                continue
            try:
                text = _decode_text(oto_path)
                stat = oto_path.stat()
                digest.update(str(oto_path.relative_to(base)).encode("utf-8", errors="replace"))
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
            except OSError as exc:
                raise TetoVoicebankError(f"não foi possível ler {oto_path}: {exc}") from exc

            for line in text.splitlines():
                clean = line.strip().lstrip("\ufeff")
                if not clean or clean.startswith(("#", ";")) or "=" not in clean:
                    continue
                wav_name, payload = clean.split("=", 1)
                values = [part.strip() for part in payload.split(",")]
                while len(values) < 6:
                    values.append("")
                alias = _normalized_alias(values[0] or Path(wav_name).stem)
                if not alias:
                    continue
                wav_path = (oto_path.parent / wav_name.strip()).resolve()
                if not _is_within(wav_path, base) or not wav_path.is_file():
                    continue
                entry = OtoEntry(
                    alias=alias,
                    wav_path=wav_path,
                    offset_ms=max(0.0, _number(values[1])),
                    consonant_ms=max(0.0, _number(values[2])),
                    cutoff_ms=_number(values[3]),
                    preutterance_ms=max(0.0, _number(values[4])),
                    overlap_ms=max(0.0, _number(values[5])),
                )
                entries.setdefault(alias, entry)
                if suffix and alias.endswith(suffix):
                    # Keep the actual recorded alias in OtoEntry/cache keys.
                    # The phonemizer uses canonical phones; prefix.map's pitch
                    # suffix is a bank setting, never an extra phonetic symbol.
                    canonical = _normalized_alias(alias[:-len(suffix)])
                    if canonical:
                        entries.setdefault(canonical, entry)
                folded = _normalized_alias(alias).casefold()
                previous = folded_candidates.get(folded, ...)
                if previous is ...:
                    folded_candidates[folded] = entry
                elif previous is not None and previous.alias != alias:
                    # X-SAMPA is case-sensitive (E/e, I/i, O/o, U/u). Never
                    # let a convenience case-fold silently replace one phoneme
                    # with another. Keep case-insensitive fallback only when the
                    # folded spelling is unambiguous across the bank.
                    folded_candidates[folded] = None
                digest.update(entry.cache_identity().encode("utf-8", errors="replace"))

        unique_entries = {id(entry): entry for entry in entries.values()}
        if len(unique_entries) < max(1, int(minimum_aliases)):
            raise TetoVoicebankError(
                f"voicebank possui poucos aliases válidos ({len(unique_entries)} < {minimum_aliases})"
            )

        name = "Kasane Teto"
        character = base / "character.txt"
        if not character.is_file():
            character = next(iter(sorted(base.rglob("character.txt"))), character)
        if character.is_file():
            try:
                for line in _decode_text(character).splitlines():
                    if line.lower().startswith("name="):
                        candidate = line.split("=", 1)[1].strip()
                        if candidate:
                            name = candidate[:120]
                        break
            except OSError:
                pass

        folded_entries = {} if case_sensitive else {
            key: entry for key, entry in folded_candidates.items() if entry is not None
        }
        return cls(base, entries, folded_entries, digest.hexdigest(), name, pitch_map=mapped_pitches)

    @property
    def alias_count(self) -> int:
        return len({id(entry) for entry in self.entries.values()})

    def _lookup(self, key: str) -> OtoEntry | None:
        return self.entries.get(key) or self.folded_entries.get(key.casefold())

    def _canonical_pitch_alias(self, key: str) -> str:
        # Lexical inventory probes normally pass an unsuffixed alias. Also
        # tolerate a previously resolved alias without permanently pinning its
        # register. Strip only authored affixes with a real phonetic family.
        affixes = sorted(self._pitch_registers, key=lambda pair: -(len(pair[0]) + len(pair[1])))
        for prefix, suffix in affixes:
            if not (prefix or suffix) or not key.startswith(prefix) or not key.endswith(suffix):
                continue
            end = len(key) - len(suffix) if suffix else len(key)
            canonical = key[len(prefix):end]
            if canonical and any(self._lookup(left + canonical + right) is not None
                                 for left, right in self._pitch_registers):
                return canonical
        return key

    def resolve(self, candidates: Iterable[str], *, pitch: str | None = None) -> OtoEntry | None:
        number = _pitch_number(pitch) if pitch is not None else None
        if pitch is not None and self.pitch_map and number is None:
            raise TetoVoicebankError("nota inválida para escolher o registro da voicebank")
        register_order = ()
        if self.pitch_map:
            if number is None:
                # A lexical probe uses the ordinary unmarked recording when
                # present. Return its canonical spelling below, never a B3/A4
                # suffix that would freeze every subsequent rendered note.
                register_order = sorted(self._pitch_registers,
                                        key=lambda pair: (bool(pair[0] or pair[1]), pair))
            else:
                target = self.pitch_map.get(number)
                register_order = sorted(self._pitch_registers, key=lambda pair: (
                    pair != target if target is not None else False,
                    min(abs(number - reference) for reference in self._pitch_registers[pair]),
                    pair,
                ))
        for candidate in candidates:
            key = _normalized_alias(candidate)
            if not key:
                continue
            if self.pitch_map:
                canonical = self._canonical_pitch_alias(key)
                for prefix, suffix in register_order:
                    entry = self._lookup(prefix + canonical + suffix)
                    if entry is not None:
                        return replace(entry, alias=canonical) if number is None and entry.alias != canonical else entry
                continue
            entry = self._lookup(key)
            if entry is not None:
                return entry
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "root": str(self.root),
            "aliases": self.alias_count,
            "fingerprint": self.fingerprint,
        }
