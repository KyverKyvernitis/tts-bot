from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
PYTHON_ROOT = ANDROID / "app/src/main/python"
SELF_BUILDER = PYTHON_ROOT / "coreworker/apk_self_builder.py"


def _load_self_builder(name: str = "apk_self_builder_modularization_test"):
    sys.path.insert(0, str(PYTHON_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(name, SELF_BUILDER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(PYTHON_ROOT))


def test_public_chaquopy_api_signatures_remain_literal() -> None:
    module = _load_self_builder("apk_self_builder_api_signatures")
    assert str(inspect.signature(module.preflight)) == "(files_dir: 'str', native_dir: 'str', run_smoke: 'bool' = False) -> 'str'"
    assert str(inspect.signature(module.reconcile_interrupted_build)) == "(files_dir: 'str', job_id: 'str', attempt: 'int' = 0) -> 'str'"
    assert str(inspect.signature(module.finalize_build_attempt)) == "(files_dir: 'str', job_id: 'str', attempt: 'int' = 0) -> 'str'"
    assert str(inspect.signature(module.run)) == "(task: 'str', payload_json: 'str', files_dir: 'str', cache_dir: 'str', native_dir: 'str', server_url: 'str', worker_id: 'str', token: 'str', worker_version: 'str') -> 'str'"


@pytest.mark.parametrize(
    "manifest_patch,missing_key",
    [
        ({"versions": {"jdkMajor": "not-an-int"}}, "versions"),
        ({"validation": {"strategy": "required-executable-smoke-v2", "requiredSmokeChecks": [{"bad": True}]}}, "validation"),
    ],
)
def test_corrupt_toolchain_manifest_blocks_preflight_instead_of_escaping(
    tmp_path: Path,
    manifest_patch: dict[str, object],
    missing_key: str,
) -> None:
    module = _load_self_builder(f"apk_self_builder_corrupt_{missing_key}")
    files = tmp_path / "files"
    toolchain = files / "apk-self-builder/toolchain"
    toolchain.mkdir(parents=True)
    manifest: dict[str, object] = {
        "schema": module.TOOLCHAIN_SCHEMA_V2,
        "version": 2,
        "arch": "arm64-v8a",
        "versions": {
            "jdkMajor": 17,
            "gradle": "8.9",
            "agp": "8.7.3",
            "compileSdk": 34,
            "buildTools": "34.0.0",
            "chaquopy": "17.0.0",
        },
        "validation": {
            "strategy": "required-executable-smoke-v2",
            "requiredSmokeChecks": ["java", "javac", "jar", "gradle", "aapt2"],
        },
    }
    manifest.update(manifest_patch)
    (toolchain / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = json.loads(module.preflight(str(files), str(tmp_path / "native"), False))

    assert result["ok"] is False
    assert result["ready"] is False
    assert "toolchain" in result["missing"]
    assert result["toolchain"]["ok"] is False
    assert missing_key in result["toolchain"]["missing"]
    assert result["state"] == "apk_self_builder_blocked"
