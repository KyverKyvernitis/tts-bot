from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "android/core-worker-app/app/src/main/python"
SELF_BUILDER = PYTHON_ROOT / "coreworker/apk_self_builder.py"


def _load_self_builder(name: str):
    sys.path.insert(0, str(PYTHON_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(name, SELF_BUILDER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(PYTHON_ROOT))


def _zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members:
            archive.writestr(name, body)


def _google_services(package: str = "dev.core.worker") -> bytes:
    return json.dumps({
        "client": [{
            "client_info": {
                "android_client_info": {"package_name": package},
            },
        }],
    }).encode()


def _private_payload(*, keystore: bytes = b"keystore", alias: str = "androiddebugkey", store_password: str = "secret") -> dict[str, str]:
    google = _google_services()
    return {
        "googleServicesJsonB64": base64.b64encode(google).decode(),
        "googleServicesSha256": __import__("hashlib").sha256(google).hexdigest(),
        "googleServicesPackage": "dev.core.worker",
        "apkSigningKeystoreB64": base64.b64encode(keystore).decode(),
        "apkSigningKeystoreSha256": __import__("hashlib").sha256(keystore).hexdigest(),
        "apkSigningKeyAlias": alias,
        "apkSigningStorePassword": store_password,
    }


def test_safe_extract_zip_prevalidates_before_writing_any_member(tmp_path: Path) -> None:
    module = _load_self_builder("apk_source_prevalidate")
    archive = tmp_path / "source.zip"
    _zip(archive, [("safe/first.txt", b"first"), ("../escape.txt", b"bad")])
    target = tmp_path / "source"

    with pytest.raises(ValueError, match="caminho inseguro"):
        module._safe_extract_zip(archive, target)

    assert not (target / "safe/first.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_zip_rejects_duplicate_normalized_member(tmp_path: Path) -> None:
    module = _load_self_builder("apk_source_duplicate")
    archive = tmp_path / "source.zip"
    _zip(archive, [("app\\build.gradle", b"one"), ("app/build.gradle", b"two")])
    target = tmp_path / "source"

    with pytest.raises(ValueError, match="duplicado"):
        module._safe_extract_zip(archive, target)

    assert not (target / "app/build.gradle").exists()


def test_private_files_validate_everything_before_first_secret_write(tmp_path: Path) -> None:
    module = _load_self_builder("apk_private_atomic_validation")
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    payload = _private_payload()
    payload["apkSigningKeystoreSha256"] = "0" * 64

    with pytest.raises(ValueError, match="keystore divergente"):
        module._inject_private_files(project, payload)

    assert not (project / "app/google-services.json").exists()
    assert not (project / "app/core-worker-upload.keystore").exists()
    assert not (project / "app/core-worker-signing.properties").exists()


def test_private_signing_properties_reject_line_injection(tmp_path: Path) -> None:
    module = _load_self_builder("apk_private_properties")
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    payload = _private_payload(alias="androiddebugkey\nINJECTED=value")

    with pytest.raises(ValueError, match="assinatura compatível"):
        module._inject_private_files(project, payload)

    assert not (project / "app/core-worker-signing.properties").exists()


def test_runtime_asset_hydration_does_not_follow_repro_symlinks(tmp_path: Path) -> None:
    module = _load_self_builder("apk_hydration_symlink")
    project = tmp_path / "project"
    native = tmp_path / "native"
    repro = tmp_path / "repro"
    project.mkdir(); native.mkdir(); repro.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside-secret")
    (repro / "leak.txt").symlink_to(outside)

    result = module._hydrate_runtime_assets(project, native, repro)

    assert not (project / "app/src/main/assets/leak.txt").exists()
    assert result["count"] == 0


def test_runtime_asset_hydration_refreshes_changed_same_size_file(tmp_path: Path) -> None:
    module = _load_self_builder("apk_hydration_same_size")
    project = tmp_path / "project"
    native = tmp_path / "native"
    repro = tmp_path / "repro"
    native.mkdir(); repro.mkdir()
    source = repro / "runtime.bin"
    source.write_bytes(b"new!")
    target = project / "app/src/main/assets/runtime.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old!")

    result = module._hydrate_runtime_assets(project, native, repro)

    assert target.read_bytes() == b"new!"
    assert "app/src/main/assets/runtime.bin" in result["copied"]
