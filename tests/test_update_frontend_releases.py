from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _frontend_functions(source: str) -> str:
    start = source.index("frontend_release_root_for_key() {")
    end = source.index("\ndeploy_frontend() {", start)
    return source[start:end]


def _harness_prelude(functions: Path, publish: Path, releases: Path) -> str:
    return f'''\nsource "{functions}"\nFRONT_PUBLISH_DIR="{publish}"\nFRONT_RELEASE_ROOT="{releases}"\nFRONT_RELEASE_RETENTION=2\nRUNTIME_RELEASE_ROOT="{publish.parent / 'runtime-releases'}"\nLOG_TAG=test\nFRONT_STATUS=''\nLAST_ERROR_STDERR=''\nsanitize_commit_ref() {{ printf '%s\\n' "$1"; }}\nshort_commit() {{ printf '%.7s' "$1"; }}\nlogger() {{ :; }}\nrepo_git() {{ printf '%s\\n' deadbeef; }}\n'''


def test_first_frontend_adoption_renames_existing_tree_without_copy(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "front-functions.sh"
    functions.write_text(_frontend_functions(source), encoding="utf-8")
    publish = tmp_path / "www" / "sinuca"
    releases = tmp_path / "www" / "sinuca-releases"
    publish.mkdir(parents=True)
    index = publish / "index.html"
    index.write_text("old", encoding="utf-8")
    inode_before = os.stat(index).st_ino

    harness = _harness_prelude(functions, publish, releases) + '''
key="$(adopt_frontend_publication_as_release deadbeef)"
printf 'KEY=%s\n' "$key"
test -L "$FRONT_PUBLISH_DIR"
test "$(frontend_active_release_key)" = deadbeef
test "$(cat "$FRONT_PUBLISH_DIR/index.html")" = old
'''
    result = _run_bash(harness)
    assert "KEY=deadbeef" in result.stdout
    release_index = releases / "deadbeef" / "index.html"
    assert release_index.is_file()
    assert os.stat(release_index).st_ino == inode_before
    assert publish.is_symlink()


def test_prepared_frontend_release_uses_hardlinks_when_same_filesystem(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "front-functions.sh"
    functions.write_text(_frontend_functions(source), encoding="utf-8")
    publish = tmp_path / "www" / "sinuca"
    releases = tmp_path / "www" / "sinuca-releases"
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "index.html").write_text("new", encoding="utf-8")
    inode_before = os.stat(artifact / "index.html").st_ino

    harness = _harness_prelude(functions, publish, releases) + f'''
prepare_frontend_release "{artifact}" cafebabe >/dev/null
verify_frontend_release "{releases / 'cafebabe'}" cafebabe
'''
    _run_bash(harness)
    assert os.stat(releases / "cafebabe" / "index.html").st_ino == inode_before


def test_publish_and_rollback_are_symlink_switches(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "front-functions.sh"
    functions.write_text(_frontend_functions(source), encoding="utf-8")
    publish = tmp_path / "www" / "sinuca"
    releases = tmp_path / "www" / "sinuca-releases"
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir(); new.mkdir()
    (old / "index.html").write_text("old", encoding="utf-8")
    (new / "index.html").write_text("new", encoding="utf-8")

    harness = _harness_prelude(functions, publish, releases) + f'''
prepare_frontend_release "{old}" deadbeef >/dev/null
prepare_frontend_release "{new}" cafebabe >/dev/null
activate_frontend_release deadbeef
test "$(cat "$FRONT_PUBLISH_DIR/index.html")" = old
activate_frontend_release cafebabe
test "$(cat "$FRONT_PUBLISH_DIR/index.html")" = new
printf 'NEW=%s\n' "$(frontend_active_release_key)"
activate_frontend_release deadbeef
test "$(cat "$FRONT_PUBLISH_DIR/index.html")" = old
printf 'OLD=%s\n' "$(frontend_active_release_key)"
'''
    result = _run_bash(harness)
    assert "NEW=cafebabe" in result.stdout
    assert "OLD=deadbeef" in result.stdout
    assert publish.is_symlink()


def test_frontend_release_pruner_keeps_active_and_rollback_references(tmp_path: Path) -> None:
    source = UPDATER.read_text(encoding="utf-8")
    functions = tmp_path / "front-functions.sh"
    functions.write_text(_frontend_functions(source), encoding="utf-8")
    publish = tmp_path / "www" / "sinuca"
    releases = tmp_path / "www" / "sinuca-releases"
    runtime_releases = publish.parent / "runtime-releases"
    keys = [f"{n:08x}" for n in range(1, 6)]
    for idx, key in enumerate(keys, 1):
        release = releases / key
        release.mkdir(parents=True)
        (release / "index.html").write_text(key, encoding="utf-8")
        subprocess.run(["touch", "-d", f"2026-01-0{idx} 00:00:00", str(release)], check=True)
    active = keys[0]
    rollback_ref = keys[1]
    runtime = runtime_releases / "commit-old"
    runtime.mkdir(parents=True)
    (runtime / "release.json").write_text(
        json.dumps({"state": "ready", "frontend": {"ready": True, "release_key": rollback_ref}}),
        encoding="utf-8",
    )

    # Manifests das releases são criados pelo helper para garantir integridade.
    for key in keys:
        release = releases / key
        harness = _harness_prelude(functions, publish, releases) + f'''\nwrite_frontend_release_manifest "{release}" {key}\n'''
        _run_bash(harness)
    publish.parent.mkdir(parents=True, exist_ok=True)
    publish.symlink_to(releases / active)

    harness = _harness_prelude(functions, publish, releases) + f'''
RUNTIME_RELEASE_ROOT="{runtime_releases}"
FRONT_RELEASE_RETENTION=2
prune_frontend_releases
find "{releases}" -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' | sort
'''
    result = _run_bash(harness)
    remaining = set(result.stdout.splitlines())
    assert active in remaining
    assert rollback_ref in remaining
    assert keys[-1] in remaining
    assert keys[-2] in remaining
    assert len(remaining) == 4


def test_runtime_snapshot_no_longer_copies_frontend_publication() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    snapshot = source[
        source.index("capture_runtime_release_snapshot() {") :
        source.index("\nrestore_frontend_runtime_release() {", source.index("capture_runtime_release_snapshot() {"))
    ]
    assert 'cp -a -- "$FRONT_PUBLISH_DIR"' not in snapshot
    assert '"$tmp/frontend.json"' in snapshot
    assert "adopt_frontend_publication_as_release" in snapshot


def test_frontend_publish_no_longer_renames_live_directory_on_normal_updates() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = source[
        source.index("publish_frontend_atomically() {") :
        source.index("\ncollect_protected_frontend_release_keys() {", source.index("publish_frontend_atomically() {"))
    ]
    assert "prepare_frontend_release" in block
    assert "activate_frontend_release" in block
    assert 'mv -- "$FRONT_PUBLISH_DIR" "$backup_path"' not in block
    assert "rsync -a --delete" not in block
