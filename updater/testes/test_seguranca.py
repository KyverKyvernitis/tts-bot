from __future__ import annotations

import json
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from updater.utilitarios.seguranca import (
    UpdateSecurityError,
    ZipLimits,
    build_file_integrity,
    candidate_confirmation_reasons,
    inspect_zip_archive,
    is_forbidden_update_path,
    is_safe_env_template_path,
    normalize_update_operations,
    sha256_file,
    update_operation_paths,
    update_payload_paths,
    verify_candidate,
)


def test_candidate_confirmation_reasons_only_cover_confirmable_scope_risks() -> None:
    large = [f"cogs/module_{index}.py" for index in range(121)]
    assert candidate_confirmation_reasons("patch.zip", large) == [
        "muitos arquivos alterados para um patch normal (121)"
    ]

    repo_reasons = candidate_confirmation_reasons("repo-20260916.zip", ["cogs/a.py"])
    assert repo_reasons == ["o arquivo parece uma base completa, não um patch"]

    frontend = [f"dashboard/file_{index}.ts" for index in range(19)] + [
        "dashboard/frontend/package-lock.json",
        "dashboard/backend/package-lock.json",
    ]
    assert "parece conter árvore de projeto/frontend completa" in candidate_confirmation_reasons(
        "frontend.zip", frontend
    )

    assert candidate_confirmation_reasons("patch.zip", ["cogs/a.py"]) == []


def test_confirmation_does_not_turn_structurally_forbidden_paths_into_soft_risks() -> None:
    assert candidate_confirmation_reasons("patch.zip", [".env"]) == []
    assert is_forbidden_update_path(".env")
    with pytest.raises(UpdateSecurityError, match="protegido"):
        normalize_update_operations([{"op": "update", "path": ".env"}])


def _write_zip(path: Path, entries: list[tuple[str, bytes]], *, compression=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def test_inspect_zip_accepts_small_patch(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "patch.zip", [("cogs/example.py", b"print('ok')\n")])
    result = inspect_zip_archive(archive)
    assert result.files == 1
    assert result.entries == 1
    assert result.uncompressed_bytes == len(b"print('ok')\n")
    assert result.sha256 == sha256_file(archive)


@pytest.mark.parametrize("name", ["../bot.py", "/etc/passwd", "C:/secret.txt", "folder/../../bot.py"])
def test_inspect_zip_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = _write_zip(tmp_path / "unsafe.zip", [(name, b"x")])
    with pytest.raises(UpdateSecurityError):
        inspect_zip_archive(archive)


@pytest.mark.parametrize("name", ["activity /sinuca/index.html", "activity/sinuca /index.html", " activity/sinuca/index.html"])
def test_inspect_zip_rejects_path_components_with_edge_whitespace(tmp_path: Path, name: str) -> None:
    archive = _write_zip(tmp_path / "whitespace.zip", [(name, b"x")])
    with pytest.raises(UpdateSecurityError, match="espaço"):
        inspect_zip_archive(archive)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template", "dashboard/backend/.env.example", "activity/sinuca-server/.env.example"])
def test_env_templates_are_allowed(name: str) -> None:
    assert is_safe_env_template_path(name)
    assert not is_forbidden_update_path(name)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.example.local",
        "dashboard/backend/.env",
        "dashboard/backend/.env.production",
        "activity/sinuca-server/.env",
        "activity/sinuca-server/.env.production",
    ],
)
def test_real_env_files_remain_forbidden(name: str) -> None:
    assert not is_safe_env_template_path(name)
    assert is_forbidden_update_path(name)


def test_inspect_zip_rejects_casefold_duplicate(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "duplicate.zip", [("cogs/Test.py", b"a"), ("cogs/test.py", b"b")])
    with pytest.raises(UpdateSecurityError, match="duplicado|ambíguo"):
        inspect_zip_archive(archive)


@pytest.mark.parametrize(
    "entries",
    [
        [("cogs", b"arquivo"), ("cogs/example.py", "conteúdo".encode())],
        [("cogs/example.py", "conteúdo".encode()), ("cogs", b"arquivo")],
    ],
)
def test_inspect_zip_rejects_file_directory_collision(tmp_path: Path, entries: list[tuple[str, bytes]]) -> None:
    archive = _write_zip(tmp_path / "collision.zip", entries)
    with pytest.raises(UpdateSecurityError, match="arquivo e diretório"):
        inspect_zip_archive(archive)


def test_inspect_zip_rejects_symlink(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link.py")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "target.py")
    with pytest.raises(UpdateSecurityError, match="symlink"):
        inspect_zip_archive(archive_path)


def test_inspect_zip_enforces_entry_file_total_and_ratio_limits(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "limits.zip", [("a.txt", b"A" * 4096), ("b.txt", b"B" * 4096)])
    with pytest.raises(UpdateSecurityError, match="entradas"):
        inspect_zip_archive(archive, ZipLimits(max_entries=1))
    with pytest.raises(UpdateSecurityError, match="arquivo"):
        inspect_zip_archive(archive, ZipLimits(max_file_bytes=1024))
    with pytest.raises(UpdateSecurityError, match="descompactado"):
        inspect_zip_archive(archive, ZipLimits(max_uncompressed_bytes=5000))
    with pytest.raises(UpdateSecurityError, match="compressão"):
        inspect_zip_archive(archive, ZipLimits(max_compression_ratio=2.0))


def _make_candidate(tmp_path: Path, *, created_at: datetime | None = None) -> Path:
    candidate = tmp_path / "candidate"
    files = candidate / "files"
    target = files / "cogs" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    patch = candidate / "patch.diff"
    patch.write_text("diff --git a/cogs/example.py b/cogs/example.py\n", encoding="utf-8")
    created = created_at or datetime.now(timezone.utc)
    manifest = {
        "schema_version": 2,
        "id": "zip-test",
        "display_id": "UPD-TEST",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=1)).isoformat(),
        "changed_files": ["cogs/example.py"],
        "file_integrity": build_file_integrity(files, ["cogs/example.py"]),
        "patch_sha256": sha256_file(patch),
        "patch_size": patch.stat().st_size,
    }
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return candidate


def test_verify_candidate_accepts_untampered_candidate(tmp_path: Path) -> None:
    candidate = _make_candidate(tmp_path)
    result = verify_candidate(candidate)
    assert result["ok"] is True
    assert result["files"] == 1
    assert result["display_id"] == "UPD-TEST"


def test_manifest_v3_accepts_delete_only_without_payload_files(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate-v3-delete"
    (candidate / "files").mkdir(parents=True)
    created = datetime.now(timezone.utc)
    operations = [{"op": "delete", "path": "cogs/obsolete.py"}]
    manifest = {
        "schema_version": 3,
        "id": "zip-v3-delete",
        "display_id": "UPD-V3DELETE",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=1)).isoformat(),
        "changed_files": update_operation_paths(operations),
        "operations": operations,
        "file_integrity": {},
    }
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_candidate(candidate)
    assert result["ok"] is True
    assert result["files"] == 0


def test_manifest_v3_integrity_covers_only_add_update_payloads(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate-v3-mixed"
    files = candidate / "files"
    payload = files / "cogs" / "new.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("VALUE = 2\n", encoding="utf-8")
    created = datetime.now(timezone.utc)
    operations = [
        {"op": "delete", "path": "cogs/obsolete.py"},
        {"op": "add", "path": "cogs/new.py"},
    ]
    manifest = {
        "schema_version": 3,
        "id": "zip-v3-mixed",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=1)).isoformat(),
        "changed_files": update_operation_paths(operations),
        "operations": operations,
        "file_integrity": build_file_integrity(files, update_payload_paths(operations)),
    }
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_candidate(candidate)["files"] == 1


def test_manifest_v3_rejects_operations_changed_files_divergence(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate-v3-bad"
    (candidate / "files").mkdir(parents=True)
    created = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 3,
        "id": "zip-v3-bad",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=1)).isoformat(),
        "changed_files": ["cogs/other.py"],
        "operations": [{"op": "delete", "path": "cogs/obsolete.py"}],
        "file_integrity": {},
    }
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(UpdateSecurityError, match="changed_files não corresponde"):
        verify_candidate(candidate)


def test_update_operations_reject_conflicts_and_protected_paths() -> None:
    with pytest.raises(UpdateSecurityError, match="conflitantes"):
        normalize_update_operations(
            [
                {"op": "delete", "path": "cogs/a.py"},
                {"op": "move", "from": "cogs/a.py", "to": "cogs/b.py"},
            ]
        )
    with pytest.raises(UpdateSecurityError, match="protegido"):
        normalize_update_operations([{"op": "delete", "path": ".env"}])
    with pytest.raises(UpdateSecurityError, match="não é permitida"):
        normalize_update_operations([{"op": "add", "path": "cogs/a.py"}], allowed_ops={"delete", "move"})


@pytest.mark.parametrize("target", ["files/cogs/example.py", "patch.diff"])
def test_verify_candidate_rejects_tampering(tmp_path: Path, target: str) -> None:
    candidate = _make_candidate(tmp_path)
    (candidate / target).write_text("alterado", encoding="utf-8")
    with pytest.raises(UpdateSecurityError, match="divergente"):
        verify_candidate(candidate)


def test_verify_candidate_rejects_expired_and_future_candidates(tmp_path: Path) -> None:
    expired = _make_candidate(tmp_path / "expired", created_at=datetime.now(timezone.utc) - timedelta(days=2))
    with pytest.raises(UpdateSecurityError, match="expirado"):
        verify_candidate(expired, max_age_seconds=3600)

    future = _make_candidate(tmp_path / "future", created_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(UpdateSecurityError, match="futuro"):
        verify_candidate(future, max_age_seconds=86400)
