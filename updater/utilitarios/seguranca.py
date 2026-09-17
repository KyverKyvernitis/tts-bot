from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


SAFE_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})
UPDATE_CONTROL_MANIFEST_NAME = "update-manifest.json"
UPDATE_OPERATION_TYPES = frozenset({"add", "update", "delete", "move"})


def is_safe_env_template_path(path: Path | PurePosixPath | str) -> bool:
    """Retorna True apenas para modelos de ambiente sem credenciais reais."""
    name = PurePosixPath(str(path).replace("\\", "/")).name.casefold()
    return name in SAFE_ENV_TEMPLATE_NAMES


def is_forbidden_update_path(path: Path | PurePosixPath | str) -> bool:
    """Política central de caminhos que um ZIP comum nunca pode sobrescrever."""
    rel = PurePosixPath(str(path).replace("\\", "/"))
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if not parts:
        return True

    lowered = tuple(part.casefold() for part in parts)
    first = lowered[0]
    name = lowered[-1]

    if first in {".git", "data", "logs", "node_modules", "secrets", "__pycache__"}:
        return True
    if first == ".github" and len(lowered) >= 2 and lowered[1] == "workflows":
        return True

    if name == ".env" or (name.startswith(".env.") and name not in SAFE_ENV_TEMPLATE_NAMES):
        return True
    if "google-credentials" in name or "youtube-cookies" in name:
        return True
    return False


def candidate_confirmation_reasons(zip_name: object, changed_files: Iterable[object]) -> list[str]:
    """Retorna heurísticas de risco que exigem aprovação humana, não rejeição.

    Segurança estrutural (traversal, caminhos protegidos, symlinks, integridade etc.)
    continua sendo bloqueante em outras validações. Aqui entram apenas sinais de que
    o pacote pode ser grande/amplo demais para aplicação automática sem revisão.
    """
    name = str(zip_name or "").strip().casefold()
    changed = [str(item).strip().replace("\\", "/") for item in changed_files if str(item).strip()]
    reasons: list[str] = []
    if name.startswith(("repo-", "tts-bot-main", "tts-bot-base")):
        reasons.append("o arquivo parece uma base completa, não um patch")
    if len(changed) > 120:
        reasons.append(f"muitos arquivos alterados para um patch normal ({len(changed)})")
    lockfiles = [path for path in changed if path.casefold().endswith("package-lock.json")]
    if len(lockfiles) >= 2 and len(changed) > 20:
        reasons.append("parece conter árvore de projeto/frontend completa")
    return list(dict.fromkeys(reasons))


class UpdateSecurityError(ValueError):
    """Raised when an update package or candidate fails a security check."""


@dataclass(frozen=True, slots=True)
class ZipLimits:
    max_archive_bytes: int = 25 * 1024 * 1024
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_entries: int = 500
    max_file_bytes: int = 20 * 1024 * 1024
    max_compression_ratio: float = 200.0


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    entries: int
    files: int
    archive_bytes: int
    uncompressed_bytes: int
    max_ratio: float
    sha256: str

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "entries": self.entries,
            "files": self.files,
            "archive_bytes": self.archive_bytes,
            "uncompressed_bytes": self.uncompressed_bytes,
            "max_ratio": round(self.max_ratio, 2),
            "sha256": self.sha256,
        }


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def normalize_member_parts(raw_name: str) -> tuple[str, ...]:
    name = str(raw_name or "").replace("\\", "/")
    if "\x00" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise UpdateSecurityError("nome com caractere de controle")
    posix = PurePosixPath(name)
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if posix.is_absolute() or any(part == ".." for part in parts):
        raise UpdateSecurityError("caminho absoluto ou traversal")
    if parts and parts[0].endswith(":"):
        raise UpdateSecurityError("caminho com unidade do Windows")
    if any(part != part.strip() for part in parts):
        raise UpdateSecurityError("componente com espaço no início ou no fim")
    return parts


def canonical_path_key(parts: Iterable[str]) -> str:
    normalized = "/".join(unicodedata.normalize("NFC", str(part)) for part in parts)
    return normalized.casefold()


def normalize_update_repo_path(raw: object) -> str:
    """Normaliza um path declarativo de update e rejeita escapes/paths protegidos."""
    text = str(raw or "").replace("\\", "/")
    try:
        parts = normalize_member_parts(text)
    except UpdateSecurityError as exc:
        raise UpdateSecurityError(f"caminho inválido em operação: {raw!r} ({exc})") from exc
    if not parts:
        raise UpdateSecurityError(f"caminho vazio em operação: {raw!r}")
    rel = PurePosixPath(*parts)
    if is_forbidden_update_path(rel):
        raise UpdateSecurityError(f"caminho protegido em operação: {rel.as_posix()}")
    return rel.as_posix()


def normalize_update_operations(
    raw_operations: object,
    *,
    allowed_ops: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    """Valida operações declarativas e retorna uma representação canônica.

    Cada path pode participar de no máximo uma operação. A primeira versão
    deliberadamente mantém `move` puro; rename+edição pode ser enviado como
    delete/add até que exista uma semântica explícita para isso.
    """
    if raw_operations is None:
        return []
    if not isinstance(raw_operations, list):
        raise UpdateSecurityError("operations precisa ser uma lista")
    if len(raw_operations) > 500:
        raise UpdateSecurityError("operations excede o limite de 500 itens")

    allowed = UPDATE_OPERATION_TYPES if allowed_ops is None else frozenset(str(item).strip().lower() for item in allowed_ops)
    unknown_allowed = allowed - UPDATE_OPERATION_TYPES
    if unknown_allowed:
        raise ValueError(f"allowed_ops inválido: {sorted(unknown_allowed)!r}")

    normalized: list[dict[str, str]] = []
    occupied: dict[str, str] = {}

    def claim(path: str, label: str) -> None:
        key = canonical_path_key(PurePosixPath(path).parts)
        previous = occupied.get(key)
        if previous is not None:
            raise UpdateSecurityError(f"path participa de operações conflitantes: {path} ({previous} / {label})")
        occupied[key] = label

    for index, raw in enumerate(raw_operations):
        if not isinstance(raw, Mapping):
            raise UpdateSecurityError(f"operação #{index + 1} precisa ser um objeto")
        op = str(raw.get("op") or "").strip().lower()
        if op not in UPDATE_OPERATION_TYPES:
            raise UpdateSecurityError(f"operação desconhecida: {op or '<vazia>'}")
        if op not in allowed:
            raise UpdateSecurityError(f"operação {op!r} não é permitida neste manifesto")

        if op == "move":
            source = normalize_update_repo_path(raw.get("from"))
            target = normalize_update_repo_path(raw.get("to"))
            if canonical_path_key(PurePosixPath(source).parts) == canonical_path_key(PurePosixPath(target).parts):
                raise UpdateSecurityError(f"move precisa ter origem e destino diferentes: {source}")
            claim(source, f"move.from#{index + 1}")
            claim(target, f"move.to#{index + 1}")
            normalized.append({"op": "move", "from": source, "to": target})
            continue

        path = normalize_update_repo_path(raw.get("path"))
        claim(path, f"{op}#{index + 1}")
        normalized.append({"op": op, "path": path})

    return normalized


def update_operation_paths(operations: Iterable[Mapping[str, object]]) -> list[str]:
    paths: list[str] = []
    for item in operations:
        op = str(item.get("op") or "").strip().lower()
        if op == "move":
            paths.extend([str(item.get("from") or ""), str(item.get("to") or "")])
        else:
            paths.append(str(item.get("path") or ""))
    return [path for path in paths if path]


def update_payload_paths(operations: Iterable[Mapping[str, object]]) -> list[str]:
    return [
        str(item.get("path") or "")
        for item in operations
        if str(item.get("op") or "").strip().lower() in {"add", "update"} and str(item.get("path") or "")
    ]


def _entry_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0o170000
    if info.is_dir() or mode == stat.S_IFDIR:
        return "dir"
    if mode in (0, stat.S_IFREG):
        return "file"
    if mode == stat.S_IFLNK:
        return "symlink"
    return "special"


def inspect_zip_archive(path: Path, limits: ZipLimits | None = None) -> ArchiveInspection:
    limits = limits or ZipLimits()
    archive_size = path.stat().st_size
    if archive_size > limits.max_archive_bytes:
        raise UpdateSecurityError(
            f"ZIP excede o limite compactado de {limits.max_archive_bytes // (1024 * 1024)} MB"
        )

    seen: dict[str, tuple[str, str]] = {}
    total_uncompressed = 0
    files = 0
    max_ratio = 0.0

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise UpdateSecurityError(f"ZIP contém {len(infos)} entradas; limite: {limits.max_entries}")

        for info in infos:
            try:
                parts = normalize_member_parts(info.filename)
            except UpdateSecurityError as exc:
                raise UpdateSecurityError(f"caminho inválido no ZIP: {info.filename!r} ({exc})") from exc
            if not parts:
                continue

            kind = _entry_kind(info)
            if kind == "symlink":
                raise UpdateSecurityError(f"symlink não permitido: {info.filename}")
            if kind == "special":
                raise UpdateSecurityError(f"arquivo especial não permitido: {info.filename}")

            key = canonical_path_key(parts)
            previous = seen.get(key)
            if previous is not None:
                previous_name, previous_kind = previous
                raise UpdateSecurityError(
                    f"caminho duplicado ou ambíguo: {previous_name!r} e {info.filename!r} ({previous_kind}/{kind})"
                )
            ancestor_keys = [canonical_path_key(parts[:index]) for index in range(1, len(parts))]
            for ancestor in ancestor_keys:
                prior = seen.get(ancestor)
                if prior is not None and prior[1] == "file":
                    raise UpdateSecurityError(
                        f"conflito entre arquivo e diretório: {prior[0]!r} e {info.filename!r}"
                    )
            if kind == "file" and any(existing.startswith(key + "/") for existing in seen):
                descendant = next(existing for existing in seen if existing.startswith(key + "/"))
                raise UpdateSecurityError(
                    f"conflito entre arquivo e diretório: {info.filename!r} e {seen[descendant][0]!r}"
                )
            seen[key] = (info.filename, kind)

            if kind == "dir":
                continue

            files += 1
            if info.file_size > limits.max_file_bytes:
                raise UpdateSecurityError(
                    f"arquivo {info.filename!r} excede {limits.max_file_bytes // (1024 * 1024)} MB"
                )
            total_uncompressed += int(info.file_size)
            if total_uncompressed > limits.max_uncompressed_bytes:
                raise UpdateSecurityError(
                    f"conteúdo descompactado excede {limits.max_uncompressed_bytes // (1024 * 1024)} MB"
                )

            if info.file_size > 0:
                if info.compress_size <= 0:
                    raise UpdateSecurityError(f"taxa de compressão inválida em {info.filename!r}")
                ratio = float(info.file_size) / float(info.compress_size)
                max_ratio = max(max_ratio, ratio)
                if ratio > limits.max_compression_ratio:
                    raise UpdateSecurityError(
                        f"taxa de compressão suspeita em {info.filename!r}: {ratio:.1f}x"
                    )

    if files == 0:
        raise UpdateSecurityError("o ZIP não contém arquivos")

    return ArchiveInspection(
        entries=len(seen),
        files=files,
        archive_bytes=archive_size,
        uncompressed_bytes=total_uncompressed,
        max_ratio=max_ratio,
        sha256=sha256_file(path),
    )


def build_file_integrity(files_dir: Path, changed_files: Iterable[str]) -> dict[str, dict[str, int | str]]:
    root = files_dir.resolve()
    result: dict[str, dict[str, int | str]] = {}
    seen: set[str] = set()
    for raw in changed_files:
        rel = PurePosixPath(str(raw).replace("\\", "/"))
        if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
            raise UpdateSecurityError(f"caminho inválido no manifesto: {raw!r}")
        key = canonical_path_key(rel.parts)
        if key in seen:
            raise UpdateSecurityError(f"caminho duplicado no manifesto: {raw!r}")
        seen.add(key)
        path = (files_dir / Path(*rel.parts)).resolve()
        if root not in path.parents:
            raise UpdateSecurityError(f"arquivo fora do candidato: {raw!r}")
        if not path.is_file() or path.is_symlink():
            raise UpdateSecurityError(f"arquivo ausente ou inválido no candidato: {raw!r}")
        result[rel.as_posix()] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return result


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def verify_candidate(candidate_dir: Path, *, max_age_seconds: int = 86400) -> dict[str, object]:
    candidate_dir = candidate_dir.resolve()
    manifest_path = candidate_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise UpdateSecurityError("manifest.json ausente")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpdateSecurityError(f"manifest.json inválido: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise UpdateSecurityError("manifest.json precisa ser um objeto")

    candidate_id = str(manifest.get("id") or "").strip()
    if not candidate_id:
        raise UpdateSecurityError("ID do candidato ausente")

    created_at = _parse_timestamp(manifest.get("created_at"))
    expires_at = _parse_timestamp(manifest.get("expires_at"))
    now = datetime.now(timezone.utc)
    if created_at is None:
        raise UpdateSecurityError("created_at ausente ou inválido")
    if (created_at - now).total_seconds() > 300:
        raise UpdateSecurityError("created_at está no futuro")
    if max_age_seconds > 0 and (now - created_at).total_seconds() > max_age_seconds:
        raise UpdateSecurityError("candidato expirado pela idade máxima")
    if expires_at is not None and now >= expires_at:
        raise UpdateSecurityError("candidato expirado")

    changed_files = [str(item).strip() for item in (manifest.get("changed_files") or []) if str(item).strip()]
    if not changed_files:
        raise UpdateSecurityError("lista changed_files vazia")

    try:
        schema_version = int(manifest.get("schema_version") or 2)
    except (TypeError, ValueError) as exc:
        raise UpdateSecurityError("schema_version inválido") from exc

    payload_files = changed_files
    if schema_version >= 3:
        operations = normalize_update_operations(manifest.get("operations"))
        if not operations:
            raise UpdateSecurityError("operations vazia no manifesto v3")
        expected_changed = update_operation_paths(operations)
        changed_keys = [canonical_path_key(PurePosixPath(normalize_update_repo_path(item)).parts) for item in changed_files]
        expected_keys = [canonical_path_key(PurePosixPath(item).parts) for item in expected_changed]
        if len(changed_keys) != len(set(changed_keys)):
            raise UpdateSecurityError("changed_files contém caminhos duplicados")
        if set(changed_keys) != set(expected_keys):
            raise UpdateSecurityError("changed_files não corresponde às operations")
        payload_files = update_payload_paths(operations)

    integrity = manifest.get("file_integrity")
    if not isinstance(integrity, Mapping):
        raise UpdateSecurityError("file_integrity ausente")
    actual = build_file_integrity(candidate_dir / "files", payload_files)
    if set(actual) != set(str(key) for key in integrity):
        raise UpdateSecurityError("file_integrity não corresponde aos arquivos alterados")
    for rel, values in actual.items():
        expected = integrity.get(rel)
        if not isinstance(expected, Mapping):
            raise UpdateSecurityError(f"integridade ausente para {rel}")
        if str(expected.get("sha256") or "").lower() != str(values["sha256"]).lower():
            raise UpdateSecurityError(f"hash divergente: {rel}")
        try:
            expected_size = int(expected.get("size"))
        except (TypeError, ValueError) as exc:
            raise UpdateSecurityError(f"tamanho inválido: {rel}") from exc
        if expected_size != values["size"]:
            raise UpdateSecurityError(f"tamanho divergente: {rel}")

    patch_path = candidate_dir / "patch.diff"
    expected_patch_hash = str(manifest.get("patch_sha256") or "").strip().lower()
    if patch_path.exists():
        if not patch_path.is_file() or patch_path.is_symlink():
            raise UpdateSecurityError("patch.diff inválido")
        actual_patch_hash = sha256_file(patch_path)
        if expected_patch_hash and actual_patch_hash != expected_patch_hash:
            raise UpdateSecurityError("hash divergente: patch.diff")
        try:
            expected_patch_size = int(manifest.get("patch_size") or 0)
        except (TypeError, ValueError) as exc:
            raise UpdateSecurityError("patch_size inválido") from exc
        if expected_patch_size and patch_path.stat().st_size != expected_patch_size:
            raise UpdateSecurityError("tamanho divergente: patch.diff")
    elif expected_patch_hash:
        raise UpdateSecurityError("patch.diff ausente")

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "display_id": str(manifest.get("display_id") or candidate_id),
        "files": len(actual),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def _cmd_inspect_zip(args: argparse.Namespace) -> int:
    limits = ZipLimits(
        max_archive_bytes=args.max_archive_bytes,
        max_uncompressed_bytes=args.max_uncompressed_bytes,
        max_entries=args.max_entries,
        max_file_bytes=args.max_file_bytes,
        max_compression_ratio=args.max_compression_ratio,
    )
    print(json.dumps(inspect_zip_archive(Path(args.path), limits).as_dict(), ensure_ascii=False))
    return 0


def _cmd_verify_candidate(args: argparse.Namespace) -> int:
    print(json.dumps(verify_candidate(Path(args.path), max_age_seconds=args.max_age_seconds), ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valida pacotes do updater do bot.")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect-zip")
    inspect_parser.add_argument("path")
    defaults = ZipLimits()
    inspect_parser.add_argument("--max-archive-bytes", type=int, default=defaults.max_archive_bytes)
    inspect_parser.add_argument("--max-uncompressed-bytes", type=int, default=defaults.max_uncompressed_bytes)
    inspect_parser.add_argument("--max-entries", type=int, default=defaults.max_entries)
    inspect_parser.add_argument("--max-file-bytes", type=int, default=defaults.max_file_bytes)
    inspect_parser.add_argument("--max-compression-ratio", type=float, default=defaults.max_compression_ratio)
    inspect_parser.set_defaults(func=_cmd_inspect_zip)

    verify_parser = sub.add_parser("verify-candidate")
    verify_parser.add_argument("path")
    verify_parser.add_argument("--max-age-seconds", type=int, default=int(os.getenv("DISCORD_AUTO_UPDATE_CANDIDATE_MAX_AGE_SECONDS", "86400")))
    verify_parser.set_defaults(func=_cmd_verify_candidate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (UpdateSecurityError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
